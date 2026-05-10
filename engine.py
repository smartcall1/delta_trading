"""통합 Delta-Neutral 엔진 — maker/taker 거래소 무관 상태머신
실전 사고(Cycle 10-13, Bug C)에서 학습한 방어 로직 전부 포함."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from typing import Optional

from config import Config
from exchanges.base import BaseExchange, Balance
from models import BotState, Cycle, Direction, Position, State
from strategy import calc_chunk_size, calc_entry_notional, select_pair_and_direction, should_exit
from telegram_ui import TelegramUI

logger = logging.getLogger(__name__)

SENTINEL_PRICE = 1e10
PRICE_RATIO_MAX = 2.0
API_FAIL_THRESHOLD = 10
IMBALANCE_ROLLBACK_PCT = 0.02
SUSPEND_MANUAL_ESCALATION_SEC = 1800


def _quantize_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(round(price / tick) * tick, decimals)


class Engine:
    def __init__(self, maker: BaseExchange, taker: BaseExchange, tg: TelegramUI, cfg: Config):
        self._maker = maker
        self._taker = taker
        self._tg = tg
        self.cfg = cfg
        self.bot_state = BotState()
        self._stop_requested = False

        self._state_path = os.path.join(cfg.LOG_DIR, "bot_state.json")
        self._cycles_path = os.path.join(cfg.LOG_DIR, "cycles.jsonl")
        self._pair_label = f"{maker.name}↔{taker.name}"

        self._spread_exit_confirms = 0
        self._pending_exit_reason: Optional[str] = None

        # Circuit breaker / API 장애 추적
        self._consecutive_api_failures = 0
        self._exit_lock = asyncio.Lock()

    # ── 라이프사이클 ──

    async def run(self):
        self.cfg.ensure_dirs(self.cfg.LOG_DIR)
        await self._maker.start()
        await self._taker.start()
        await self._tg.start()
        self._register_telegram_callbacks()

        await self._tg.send_alert(
            f"<b>[🚀 START]</b> Delta-Neutral 봇\n"
            f"📡 {self._pair_label}\n"
            f"상태: {self.bot_state.state.value}"
        )
        await self._recovery_check()

        while not self._stop_requested:
            try:
                await self._tick()
                await self._tg.poll_updates()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("엔진 루프 예외: %s", e, exc_info=True)
                await asyncio.sleep(5)

        await self._shutdown()

    async def request_stop(self):
        self._stop_requested = True

    # ── 텔레그램 ──

    def _register_telegram_callbacks(self):
        from telegram_ui import BTN_STATUS, BTN_FUNDING, BTN_HISTORY, BTN_POSITIONS, BTN_CLOSE, BTN_STOP
        self._tg.register_callback(BTN_STATUS, self._on_status)
        self._tg.register_callback(BTN_FUNDING, self._on_funding)
        self._tg.register_callback(BTN_HISTORY, self._on_history)
        self._tg.register_callback(BTN_POSITIONS, self._on_positions)
        self._tg.register_callback(BTN_CLOSE, self._on_close_now)
        self._tg.register_callback(BTN_STOP, self._on_stop)

    async def _on_status(self):
        s = self.bot_state
        pos = s.position
        lines = []
        if pos and s.state in (State.HOLD, State.HOLD_SUSPENDED):
            hold_h = pos.hold_minutes / 60
            lines.append(f"📊 #{s.cycle_count} {s.state.value} · {pos.pair} {pos.direction.value} · {hold_h:.1f}h")
        else:
            lines.append(f"📊 {s.state.value}")
        lines.append("━" * 16)

        total = None
        try:
            m_bal = await self._maker.get_balance()
            t_bal = await self._taker.get_balance()
            total = m_bal.equity + t_bal.equity
            lines.append(f"💰 <b>${total:,.0f}</b> (M ${m_bal.equity:,.0f} / T ${t_bal.equity:,.0f})")
        except Exception:
            lines.append("💰 잔고 조회 실패")

        if pos and total is not None:
            entry_total = pos.entry_total_balance
            if entry_total > 0:
                pnl = total - entry_total
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{emoji} PnL ${pnl:+,.2f}")
            lines.append(f"M: {pos.maker_size:.6f} / T: {pos.taker_size:.6f}")

        lines.append(f"📡 {self._pair_label}")
        await self._tg.send_alert("\n".join(lines))

    async def _on_funding(self):
        text = "💰 <b>Funding Rates</b>\n━━━━━━━━━━━━━━━\n"
        for pair in self.cfg.PAIRS:
            try:
                m_sym = self._maker.map_symbol(pair)
                t_sym = self._taker.map_symbol(pair)
                m_fr = await self._maker.get_funding_rate(m_sym)
                t_fr = await self._taker.get_funding_rate(t_sym)
                net_a = m_fr - t_fr
                text += f"<b>{pair}</b>: M={m_fr:+.5f} T={t_fr:+.5f} net={net_a:+.5f}\n"
            except Exception as e:
                text += f"<b>{pair}</b>: 조회 실패 ({e})\n"
        await self._tg.send_alert(text)

    async def _on_history(self):
        if not os.path.exists(self._cycles_path):
            await self._tg.send_alert("📋 사이클 없음")
            return
        try:
            with open(self._cycles_path) as f:
                lines = f.readlines()[-5:]
        except Exception:
            await self._tg.send_alert("📋 기록 읽기 실패")
            return
        text = "📋 <b>최근 사이클</b>\n━━━━━━━━━━━━━━━\n"
        for line in lines:
            try:
                c = json.loads(line)
                emoji = "🟢" if c.get("pnl", 0) >= 0 else "🔴"
                text += f"#{c.get('cycle_id')} {c.get('pair')} {emoji} ${c.get('pnl', 0):+.2f} | {c.get('exit_reason', '?')}\n"
            except Exception:
                continue
        await self._tg.send_alert(text)

    async def _on_positions(self):
        text = "📌 <b>Positions</b>\n━━━━━━━━━━━━━━━\n"
        for pair in self.cfg.PAIRS:
            try:
                m_sym = self._maker.map_symbol(pair)
                t_sym = self._taker.map_symbol(pair)
                m_size = await self._maker.get_position_signed_size(m_sym)
                t_size = await self._taker.get_position_signed_size(t_sym)
                if abs(m_size) > 1e-8 or abs(t_size) > 1e-8:
                    text += f"<b>{pair}</b>: M={m_size:+.6f} T={t_size:+.6f}\n"
            except Exception:
                pass
        await self._tg.send_alert(text)

    async def _on_close_now(self):
        pos = self.bot_state.position
        if not pos or self.bot_state.state not in (State.HOLD, State.HOLD_SUSPENDED, State.MANUAL_INTERVENTION):
            await self._tg.send_alert("현재 상태에서는 강제 청산 불가")
            return
        if await self._positions_at_dust(pos):
            await self._tg.send_alert("<b>[🔚 CLOSE NOW]</b> 이미 dust 이하 — 사이클 종료")
            await self._finalize_cycle(pos, reason_override="manual_close_now")
            return
        pos.exit_reason = "manual_close_now"
        self._transition(State.EXIT)
        await self._tg.send_alert("<b>[🔚 CLOSE NOW]</b> 강제 청산 시작")

    async def _on_stop(self):
        await self._tg.send_alert("<b>[⏹ STOP]</b> 봇 종료 요청")
        self._stop_requested = True

    # ── 상태 디스패치 ──

    async def _tick(self):
        s = self.bot_state.state
        if s == State.IDLE:
            await self._state_idle()
        elif s == State.ANALYZE:
            await self._state_analyze()
        elif s == State.ENTER:
            await self._state_enter()
        elif s == State.HOLD:
            await self._state_hold()
        elif s == State.HOLD_SUSPENDED:
            await self._state_hold_suspended()
        elif s == State.EXIT:
            await self._state_exit()
        elif s == State.COOLDOWN:
            await self._state_cooldown()
        elif s == State.MANUAL_INTERVENTION:
            await self._state_manual()

    # ── Strict Position Query (Cycle 11/12/13 패치) ──

    async def _get_position_strict(self, client: BaseExchange, symbol: str) -> Optional[float]:
        """API 실패 시 None 반환 (포지션 없음=0.0과 구분)."""
        try:
            return await client.get_position_signed_size(symbol)
        except Exception:
            return None

    # ── IDLE ──

    async def _state_idle(self):
        funding = {}
        for pair in self.cfg.PAIRS:
            try:
                m_sym = self._maker.map_symbol(pair)
                t_sym = self._taker.map_symbol(pair)
                m_fr = await self._maker.get_funding_rate(m_sym)
                t_fr = await self._taker.get_funding_rate(t_sym)
                funding[pair] = {"maker": m_fr, "taker": t_fr}
                logger.info("펀딩 %s: M=%.5f T=%.5f", pair, m_fr, t_fr)
            except Exception as e:
                logger.warning("펀딩 조회 실패 %s: %s", pair, e)

        if not funding:
            await asyncio.sleep(10)
            return

        pair, direction, net = select_pair_and_direction(funding)
        logger.info("선택: %s %s net=%.5f", pair, direction.value, net)

        try:
            m_bal = await self._maker.get_balance()
            equity = m_bal.equity
        except Exception as e:
            logger.warning("Maker 잔고 조회 실패: %s", e)
            await asyncio.sleep(5)
            return

        notional = calc_entry_notional(equity, self.cfg.LEVERAGE)

        # 가격 조회 + 센티넬 가격 방어
        try:
            m_sym = self._maker.map_symbol(pair)
            t_sym = self._taker.map_symbol(pair)
            bbo = await self._maker.get_bbo(m_sym)
            m_price = bbo["mark"]
            t_price = await self._taker.get_mark_price(t_sym)
        except Exception:
            await asyncio.sleep(5)
            return

        if m_price <= 0 or m_price > SENTINEL_PRICE or t_price <= 0 or t_price > SENTINEL_PRICE:
            logger.warning("센티넬/비정상 가격 감지: maker=%.2f taker=%.2f", m_price, t_price)
            await asyncio.sleep(10)
            return

        # 가격비율 가드
        price_ratio = max(m_price, t_price) / min(m_price, t_price) if min(m_price, t_price) > 0 else 999
        if price_ratio > PRICE_RATIO_MAX:
            logger.warning("가격 괴리 %.2fx — 진입 보류", price_ratio)
            await asyncio.sleep(30)
            return

        try:
            t_bal = await self._taker.get_balance()
            t_equity = t_bal.equity
        except Exception:
            t_equity = 0

        self.bot_state.position = Position(
            pair=pair, direction=direction,
            entry_balance=equity, entry_total_balance=equity + t_equity,
            target_notional=notional, avg_entry_price=m_price,
        )
        self._transition(State.ANALYZE)

    # ── ANALYZE ──

    async def _state_analyze(self):
        # Stop 레이스 컨디션 방어
        if self._stop_requested:
            self.bot_state.position = None
            self._transition(State.IDLE)
            return

        pos = self.bot_state.position
        if not pos:
            self._transition(State.IDLE)
            return

        logger.info("ANALYZE: %s %s notional=$%.0f", pos.pair, pos.direction.value, pos.target_notional)

        t_sym = self._taker.map_symbol(pos.pair)
        try:
            await self._taker.set_leverage(t_sym, self.cfg.LEVERAGE)
            logger.info("Taker leverage=%dx 설정: %s", self.cfg.LEVERAGE, t_sym)
        except Exception as e:
            logger.warning("Taker 레버리지 설정 실패: %s", e)

        m_sym = self._maker.map_symbol(pos.pair)
        try:
            await self._maker.set_leverage(m_sym, self.cfg.LEVERAGE)
        except Exception:
            pass

        # Stop 재확인 (ANALYZE→ENTER 전환점)
        if self._stop_requested:
            self.bot_state.position = None
            self._transition(State.IDLE)
            return

        self._transition(State.ENTER)

    # ── ENTER ──

    async def _state_enter(self):
        pos = self.bot_state.position
        if not pos:
            self._transition(State.IDLE)
            return

        logger.info("ENTER: %s %s", pos.pair, pos.direction.value)

        enter_deadline = time.time() + 60 * 60
        chunk_idx = pos.chunks_filled + 1
        concession_carry = 0.0

        while chunk_idx <= self.cfg.ENTRY_CHUNKS:
            if self._stop_requested:
                break
            if time.time() > enter_deadline:
                break
            success, concession_carry = await self._xemm_chunk_enter(
                pos=pos, chunk_idx=chunk_idx, total_chunks=self.cfg.ENTRY_CHUNKS,
                concession_carry=concession_carry,
            )
            if success:
                concession_carry = 0.0
                chunk_idx += 1
            else:
                await asyncio.sleep(min(30, 10 * (chunk_idx - pos.chunks_filled)))

        # 실측 동기화 + 불균형 조정
        await self._sync_and_rebalance(pos)

        if pos.maker_size > 0:
            await self._tg.send_alert(
                f"<b>[✅ ENTER]</b> {pos.pair} {pos.direction.value}\n"
                f"📡 {self._pair_label}\n"
                f"M: {pos.maker_size:.6f} / T: {pos.taker_size:.6f}\n"
                f"청크: {pos.chunks_filled}/{self.cfg.ENTRY_CHUNKS}"
            )
            self._transition(State.HOLD)
        else:
            self.bot_state.position = None
            self._transition(State.COOLDOWN)

    async def _sync_and_rebalance(self, pos: Position):
        m_sym = self._maker.map_symbol(pos.pair)
        t_sym = self._taker.map_symbol(pos.pair)

        real_m = await self._get_position_strict(self._maker, m_sym)
        real_t = await self._get_position_strict(self._taker, t_sym)
        if real_m is None or real_t is None:
            logger.warning("실측 동기화 실패 — strict 조회 None")
            return

        m_abs, t_abs = abs(real_m), abs(real_t)

        # 2% 초과 불균형 시 전량 롤백 (Bug C 방어)
        if m_abs > 0 and t_abs > 0:
            imbalance_pct = abs(m_abs - t_abs) / min(m_abs, t_abs)
            if imbalance_pct > IMBALANCE_ROLLBACK_PCT:
                logger.warning("진입 후 불균형 %.1f%%: maker=%.6f taker=%.6f", imbalance_pct * 100, m_abs, t_abs)
                if m_abs > t_abs:
                    close_side = "SELL" if pos.maker_side == "BUY" else "BUY"
                    await self._rollback_with_retry(self._maker, m_sym, close_side, m_abs - t_abs)
                else:
                    close_side = "SELL" if pos.taker_side == "BUY" else "BUY"
                    await self._rollback_with_retry(self._taker, t_sym, close_side, t_abs - m_abs)
                await asyncio.sleep(3)
                real_m = await self._get_position_strict(self._maker, m_sym)
                real_t = await self._get_position_strict(self._taker, t_sym)
                if real_m is None or real_t is None:
                    return
                m_abs, t_abs = abs(real_m), abs(real_t)

        pos.maker_size = m_abs
        pos.taker_size = t_abs

    # ── HOLD ──

    async def _state_hold(self):
        pos = self.bot_state.position
        if not pos:
            self._transition(State.IDLE)
            return
        await asyncio.sleep(30)

        if await self._positions_at_dust(pos):
            await self._finalize_cycle(pos, reason_override="external_clear")
            return

        # 잔고 조회 + API 장애 추적
        try:
            m_bal = await self._maker.get_balance()
            t_bal = await self._taker.get_balance()
            current_total = m_bal.equity + t_bal.equity
            self._consecutive_api_failures = 0
        except Exception:
            self._consecutive_api_failures += 1
            if self._consecutive_api_failures >= API_FAIL_THRESHOLD:
                logger.error("연속 API 실패 %d회 → HOLD_SUSPENDED", self._consecutive_api_failures)
                self.bot_state.suspended_since = time.time()
                self._transition(State.HOLD_SUSPENDED)
            return

        # 가격 괴리 감시
        m_sym = self._maker.map_symbol(pos.pair)
        t_sym = self._taker.map_symbol(pos.pair)
        m_mark = 0.0
        t_mark = 0.0
        try:
            m_mark = await self._maker.get_mark_price(m_sym)
            t_mark = await self._taker.get_mark_price(t_sym)
            if m_mark > SENTINEL_PRICE or t_mark > SENTINEL_PRICE:
                logger.warning("HOLD: 센티넬 가격 감지 — 스킵")
                return
            if min(m_mark, t_mark) > 0:
                ratio = max(m_mark, t_mark) / min(m_mark, t_mark)
                if ratio > PRICE_RATIO_MAX:
                    logger.error("HOLD: 가격 괴리 %.2fx — 긴급 EXIT", ratio)
                    pos.exit_reason = f"PRICE_DIVERGENCE ({ratio:.2f}x)"
                    self._transition(State.EXIT)
                    await self._tg.send_alert(
                        f"<b>[🚨 PRICE DIVERGENCE]</b> {pos.pair} ratio={ratio:.2f}x\n📡 {self._pair_label}"
                    )
                    return
        except Exception:
            pass

        # taker 마진 실측
        taker_margin_pct = 100.0
        try:
            t_pos_size = abs(await self._taker.get_position_signed_size(t_sym))
            mark_for_margin = t_mark if t_mark > 0 else m_mark
            t_notional = t_pos_size * mark_for_margin if mark_for_margin > 0 else 0
            if t_notional > 0:
                taker_margin_pct = t_bal.equity / t_notional * 100
        except Exception:
            pass

        # spread_mtm 실측 계산
        mark_for_spread = m_mark if m_mark > 0 else pos.avg_entry_price
        spread_mtm = self._calc_spread_mtm(pos, mark_for_spread)

        # 즉시 손절 (min_hold 무시)
        pnl = current_total - pos.entry_total_balance
        stoploss_threshold = -(self.cfg.PRINCIPAL_BUFFER_USD * 2)
        if pnl <= stoploss_threshold:
            pos.exit_reason = f"IMMEDIATE_STOPLOSS (${pnl:+.2f})"
            self._transition(State.EXIT)
            await self._tg.send_alert(
                f"<b>[🚨 STOPLOSS]</b> {pos.pair} ${pnl:+.2f}\n📡 {self._pair_label}"
            )
            return

        funding = await self._fetch_funding_for_pair(pos.pair)
        reason = should_exit(
            pos, current_total, taker_margin_pct=taker_margin_pct,
            spread_mtm=spread_mtm, funding=funding,
            cfg_max_hold_days=self.cfg.MAX_HOLD_DAYS,
            cfg_margin_emergency_pct=self.cfg.MARGIN_EMERGENCY_PCT,
            cfg_spread_opportunistic=self.cfg.SPREAD_OPPORTUNISTIC_USD,
            cfg_principal_buffer=self.cfg.PRINCIPAL_BUFFER_USD,
            cfg_min_hold_minutes=self.cfg.MIN_HOLD_MINUTES,
        )

        if reason:
            spread_based = reason.startswith("PRINCIPAL_RECOVERY") or reason.startswith("OPPORTUNISTIC_PROFIT")
            if spread_based:
                if self._pending_exit_reason and reason.split("(")[0] == self._pending_exit_reason.split("(")[0]:
                    self._spread_exit_confirms += 1
                else:
                    self._spread_exit_confirms = 1
                    self._pending_exit_reason = reason
                if self._spread_exit_confirms < self.cfg.SPREAD_EXIT_CONFIRM_COUNT:
                    return
                self._spread_exit_confirms = 0
                self._pending_exit_reason = None

            pos.exit_reason = reason
            self._transition(State.EXIT)
            await self._tg.send_alert(f"<b>[🔔 EXIT]</b> {pos.pair} | {reason}\n📡 {self._pair_label}")
        else:
            if self._spread_exit_confirms > 0:
                self._spread_exit_confirms = 0
                self._pending_exit_reason = None

    # ── HOLD_SUSPENDED (Circuit Breaker) ──

    async def _state_hold_suspended(self):
        pos = self.bot_state.position
        if not pos:
            self._transition(State.IDLE)
            return
        suspended_since = self.bot_state.suspended_since or time.time()
        elapsed = time.time() - suspended_since

        # 주기적 핑 (복구 시도)
        try:
            await self._maker.get_balance()
            await self._taker.get_balance()
            logger.info("HOLD_SUSPENDED: API 복구 감지 → HOLD 복귀")
            self._consecutive_api_failures = 0
            self.bot_state.suspended_since = None
            self._transition(State.HOLD)
            await self._tg.send_alert(f"<b>[✅ RECOVERED]</b> API 복구 — HOLD 복귀\n📡 {self._pair_label}")
            return
        except Exception:
            pass

        # 30분 미복구 시 MANUAL 에스컬레이션
        if elapsed > SUSPEND_MANUAL_ESCALATION_SEC:
            logger.error("HOLD_SUSPENDED 30분 초과 → MANUAL")
            self._transition(State.MANUAL_INTERVENTION)
            await self._tg.send_alert(
                f"<b>[⚠️ ESCALATION]</b> API 장애 30분 지속 → 수동 개입 필요\n📡 {self._pair_label}"
            )
            return

        # 5분마다 알림
        if int(elapsed) % 300 < 30:
            await self._tg.send_alert(
                f"<b>[⏸ SUSPENDED]</b> API 장애 {elapsed/60:.0f}분째\n📡 {self._pair_label}"
            )

        await asyncio.sleep(30)

    # ── EXIT ──

    async def _state_exit(self):
        async with self._exit_lock:
            await self._execute_exit()

    async def _execute_exit(self):
        pos = self.bot_state.position
        if not pos:
            self._transition(State.IDLE)
            return
        m_sym = self._maker.map_symbol(pos.pair)

        # cancel_all 검증
        try:
            cancel_result = await self._maker.cancel_all_orders(m_sym)
            if not cancel_result and cancel_result != 0:
                logger.warning("cancel_all 결과 의심: %s", cancel_result)
        except Exception:
            pass

        # Strict 포지션 조회
        real_m = await self._get_position_strict(self._maker, m_sym)
        if real_m is None:
            logger.error("EXIT: Maker 포지션 조회 실패 — MANUAL")
            self._transition(State.MANUAL_INTERVENTION)
            return
        total_to_exit = abs(real_m)

        if total_to_exit < 1e-8:
            await self._finalize_cycle(pos)
            return

        per_chunk = total_to_exit / self.cfg.EXIT_CHUNKS
        exited = 0.0

        for chunk_idx in range(1, self.cfg.EXIT_CHUNKS + 1):
            if await self._positions_at_dust(pos):
                break

            remain_strict = await self._get_position_strict(self._maker, m_sym)
            if remain_strict is None:
                logger.error("EXIT 청크 %d: strict 조회 실패 — 중단", chunk_idx)
                self._transition(State.MANUAL_INTERVENTION)
                return
            actual_remain = abs(remain_strict)
            remaining = min(total_to_exit - exited, actual_remain)
            if remaining < 1e-8:
                break

            chunk_size = min(per_chunk, remaining)
            exit_side = "SELL" if pos.maker_side == "BUY" else "BUY"
            filled = await self._xemm_chunk_exit(pos, chunk_idx, self.cfg.EXIT_CHUNKS, chunk_size, exit_side)
            if filled > 0:
                exited += filled
                self.bot_state.exit_failure_count = 0
            else:
                self.bot_state.exit_failure_count += 1
                if self.bot_state.exit_failure_count >= self.cfg.MAX_EXIT_FAILURES:
                    self._transition(State.MANUAL_INTERVENTION)
                    return

        if not await self._positions_at_dust(pos):
            self._transition(State.MANUAL_INTERVENTION)
            return

        await self._finalize_cycle(pos)

    # ── COOLDOWN ──

    async def _state_cooldown(self):
        logger.info("COOLDOWN %d분", self.cfg.COOLDOWN_MINUTES)
        await asyncio.sleep(self.cfg.COOLDOWN_MINUTES * 60)
        self._transition(State.IDLE)

    # ── MANUAL ──

    async def _state_manual(self):
        pos = self.bot_state.position
        if pos and await self._positions_at_dust(pos):
            await self._finalize_cycle(pos, reason_override="auto_recover")
            return
        if not pos:
            self._transition(State.IDLE)
            return
        now = time.time()
        if now - self.bot_state.last_manual_alert > 1800:
            self.bot_state.last_manual_alert = now
            m_sym = self._maker.map_symbol(pos.pair)
            t_sym = self._taker.map_symbol(pos.pair)
            m_size = await self._get_position_strict(self._maker, m_sym)
            t_size = await self._get_position_strict(self._taker, t_sym)
            await self._tg.send_alert(
                f"<b>[⚠️ MANUAL]</b> {pos.pair} 수동 개입 필요\n"
                f"📡 {self._pair_label}\n"
                f"M: {m_size} / T: {t_size}\n"
                f"실패: {self.bot_state.exit_failure_count}회"
            )
        await asyncio.sleep(60)

    # ── XEMM 청크 (진입) ──

    async def _xemm_chunk_enter(self, pos, chunk_idx, total_chunks, concession_carry=0.0):
        m_sym = self._maker.map_symbol(pos.pair)
        t_sym = self._taker.map_symbol(pos.pair)
        chunk_size = calc_chunk_size(pos.target_notional, pos.avg_entry_price, total_chunks)
        tick = self.cfg.get_tick_sizes(self._maker.name).get(pos.pair, 0.01)
        m_sign = 1.0 if pos.maker_side == "BUY" else -1.0

        concession = concession_carry

        for retry in range(self.cfg.MAKER_RETRY_LIMIT):
            if self._stop_requested:
                return False, concession

            try:
                bbo = await self._maker.get_bbo(m_sym)
            except Exception:
                await asyncio.sleep(2)
                continue

            # 센티넬 가격 방어
            if bbo["bid"] > SENTINEL_PRICE or bbo["ask"] > SENTINEL_PRICE:
                logger.warning("ENTER: 센티넬 BBO 감지")
                return False, concession

            spread = bbo["ask"] - bbo["bid"]

            if pos.maker_side == "BUY":
                raw = bbo["bid"] + concession
                raw = min(raw, bbo["ask"] - tick)
            else:
                raw = bbo["ask"] - concession
                raw = max(raw, bbo["bid"] + tick)
            price = _quantize_to_tick(raw, tick)

            # Cross 이중 방지
            if pos.maker_side == "BUY":
                price = min(price, _quantize_to_tick(bbo["ask"] - tick, tick))
            else:
                price = max(price, _quantize_to_tick(bbo["bid"] + tick, tick))

            cid = self._maker.make_client_order_id()
            size_before = await self._get_position_strict(self._maker, m_sym)
            if size_before is None:
                await asyncio.sleep(2)
                continue

            try:
                await self._maker.place_limit_order(
                    m_sym, pos.maker_side, price, chunk_size,
                    reduce_only=False, post_only=True, client_order_id=cid,
                )
            except Exception:
                await asyncio.sleep(2)
                continue

            await self._maker.wait_for_fill(cid, timeout=self.cfg.MAKER_FILL_TIMEOUT_SECONDS)

            # cancel_all 결과 검증
            cancel_ok = False
            try:
                result = await self._maker.cancel_all_orders(m_sym)
                cancel_ok = True
            except Exception as e:
                logger.warning("cancel_all 실패: %s", e)
            if not cancel_ok:
                logger.error("cancel_all 실패 — 잔여 주문 위험, 청크 중단")
                return False, concession
            await asyncio.sleep(2)

            size_after = await self._get_position_strict(self._maker, m_sym)
            if size_after is None:
                logger.warning("체결 후 포지션 strict 조회 실패")
                return False, concession
            actual_filled = max(0.0, (size_after - size_before) * m_sign)

            if actual_filled < chunk_size * 0.01:
                concession += max(self.cfg.MAKER_PRICE_STEP_USD, tick)
                concession = min(concession, spread * 0.5)
                continue

            fill_price = price

            # Taker 헷지
            try:
                t_before = await self._get_position_strict(self._taker, t_sym)
                if t_before is None:
                    raise RuntimeError("Taker strict 조회 실패")
                t_sign = 1.0 if pos.taker_side == "BUY" else -1.0
                t_tick = self.cfg.get_tick_sizes(self._taker.name).get(pos.pair, 0.1)
                slip = 1.005 if pos.taker_side == "BUY" else 0.995
                t_price = _quantize_to_tick(fill_price * slip, t_tick)
                await self._taker.place_limit_order(
                    t_sym, pos.taker_side, t_price, actual_filled, post_only=False,
                )
                t_filled = 0.0
                for poll in range(3):
                    await asyncio.sleep(3)
                    t_after = await self._get_position_strict(self._taker, t_sym)
                    if t_after is None:
                        continue
                    t_filled = max(0.0, (t_after - t_before) * t_sign)
                    if t_filled >= actual_filled * 0.9:
                        break
                if t_filled < actual_filled * 0.9:
                    raise RuntimeError(f"Taker 헷지 부족: {t_filled:.6f}/{actual_filled:.6f}")
            except Exception as e:
                logger.error("Taker 헷지 실패 → Maker 롤백: %s", e)
                rollback_side = "SELL" if pos.maker_side == "BUY" else "BUY"
                rollback_ok = await self._rollback_with_retry(self._maker, m_sym, rollback_side, actual_filled)
                if not rollback_ok:
                    await self._tg.send_alert(
                        f"<b>[🚨 편측 노출]</b> Maker 롤백 실패!\n"
                        f"📡 {self._pair_label}\n"
                        f"{pos.pair} {pos.maker_side} {actual_filled:.6f} 수동 헷지 필요"
                    )
                return False, concession

            matched = min(actual_filled, t_filled)
            pos.maker_size += matched
            pos.taker_size += matched
            pos.chunks_filled += 1
            pos.avg_entry_price = (pos.avg_entry_price * (chunk_idx - 1) + fill_price) / chunk_idx
            logger.info("청크 %d/%d 완료: %.6f @ %.2f", chunk_idx, total_chunks, matched, fill_price)
            return True, 0.0

        return False, concession

    # ── XEMM 청크 (청산) ──

    async def _xemm_chunk_exit(self, pos, chunk_idx, total_chunks, chunk_size, exit_side):
        m_sym = self._maker.map_symbol(pos.pair)
        t_sym = self._taker.map_symbol(pos.pair)
        t_close_side = "BUY" if pos.taker_side == "SELL" else "SELL"
        m_exit_sign = -1.0 if exit_side == "SELL" else 1.0
        tick = self.cfg.get_tick_sizes(self._maker.name).get(pos.pair, 0.01)

        actual_filled = 0.0
        fill_price = pos.avg_entry_price

        # Maker 제한주문 시도
        for retry in range(self.cfg.MAKER_RETRY_LIMIT):
            try:
                bbo = await self._maker.get_bbo(m_sym)
            except Exception:
                await asyncio.sleep(2)
                continue

            if bbo["bid"] > SENTINEL_PRICE or bbo["ask"] > SENTINEL_PRICE:
                return 0.0

            if exit_side == "BUY":
                price = _quantize_to_tick(bbo["ask"] - tick, tick)
            else:
                price = _quantize_to_tick(bbo["bid"] + tick, tick)

            cid = self._maker.make_client_order_id()
            size_before = await self._get_position_strict(self._maker, m_sym)
            if size_before is None:
                await asyncio.sleep(2)
                continue

            try:
                await self._maker.place_limit_order(
                    m_sym, exit_side, price, chunk_size,
                    reduce_only=True, post_only=True, client_order_id=cid,
                )
            except Exception:
                await asyncio.sleep(2)
                continue

            await self._maker.wait_for_fill(cid, timeout=self.cfg.MAKER_FILL_TIMEOUT_SECONDS)

            # cancel_all 검증
            cancel_ok = False
            try:
                await self._maker.cancel_all_orders(m_sym)
                cancel_ok = True
            except Exception:
                pass
            if not cancel_ok:
                return 0.0
            await asyncio.sleep(2)

            size_after = await self._get_position_strict(self._maker, m_sym)
            if size_after is None:
                return 0.0
            actual_filled = max(0.0, (size_before - size_after) * (-m_exit_sign))
            if actual_filled >= chunk_size * 0.01:
                fill_price = price
                break

        # 시장가 fallback
        if actual_filled < chunk_size * 0.01:
            try:
                size_before = await self._get_position_strict(self._maker, m_sym)
                if size_before is None:
                    return 0.0
                await self._maker.place_market_order(m_sym, exit_side, chunk_size, slippage=self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT)
                await asyncio.sleep(3)
                size_after = await self._get_position_strict(self._maker, m_sym)
                if size_after is None:
                    return 0.0
                actual_filled = max(0.0, (size_before - size_after) * (-m_exit_sign))
            except Exception:
                return 0.0
            if actual_filled < chunk_size * 0.01:
                return 0.0
            try:
                fill_price = (await self._maker.get_bbo(m_sym))["mark"]
            except Exception:
                pass

        # Taker 청산 + 단계적 슬리피지 (EXIT slippage escalation)
        slippages = [0.005, 0.01, self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT]
        t_tick = self.cfg.get_tick_sizes(self._taker.name).get(pos.pair, 0.1)
        t_exit_sign = -1.0 if t_close_side == "SELL" else 1.0

        # 기준점: 첫 t_before 스냅샷으로 누적 계산
        t_baseline = await self._get_position_strict(self._taker, t_sym)
        if t_baseline is None:
            await self._tg.send_alert(
                f"<b>[🚨 EXIT 불균형]</b> Taker API 장애!\n"
                f"📡 {self._pair_label}\n"
                f"Maker {actual_filled:.6f} 이미 청산됨"
            )
            return 0.0

        t_closed = 0.0
        for slip_idx, slip_pct in enumerate(slippages):
            try:
                t_price = fill_price * (1 + slip_pct) if t_close_side == "BUY" else fill_price * (1 - slip_pct)
                t_price = _quantize_to_tick(t_price, t_tick)
                remaining_to_close = actual_filled - t_closed
                if remaining_to_close < 1e-8:
                    break
                await self._taker.place_limit_order(
                    t_sym, t_close_side, t_price, remaining_to_close, reduce_only=True, post_only=False,
                )
                for poll in range(3):
                    await asyncio.sleep(3)
                    t_after = await self._get_position_strict(self._taker, t_sym)
                    if t_after is None:
                        continue
                    t_closed = max(0.0, (t_baseline - t_after) * (-t_exit_sign))
                    if t_closed >= actual_filled * 0.9:
                        break
                if t_closed >= actual_filled * 0.9:
                    break
            except Exception as e:
                logger.warning("EXIT Taker 시도 %d 실패: %s", slip_idx + 1, e)
                continue

        if t_closed < actual_filled * 0.5:
            await self._tg.send_alert(
                f"<b>[🚨 EXIT 불균형]</b> Taker 체결 부족!\n"
                f"📡 {self._pair_label}\n"
                f"Maker: {actual_filled:.6f} / Taker: {t_closed:.6f}"
            )
            logger.error("EXIT Taker 심각 부족: %.6f/%.6f", t_closed, actual_filled)
            return 0.0

        pos.maker_size = max(0.0, pos.maker_size - actual_filled)
        pos.taker_size = max(0.0, pos.taker_size - t_closed)
        logger.info("EXIT 청크 %d/%d: %.6f @ %.2f", chunk_idx, total_chunks, actual_filled, fill_price)
        return actual_filled

    # ── 롤백 재시도 (단계적 슬리피지) ──

    async def _rollback_with_retry(self, client: BaseExchange, symbol: str, side: str, size: float) -> bool:
        slippages = [
            self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT,
            self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT * 2,
            self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT * 3,
        ]
        for attempt, slip in enumerate(slippages, 1):
            try:
                real = await self._get_position_strict(client, symbol)
                if real is not None and abs(real) < 1e-8:
                    return True
                remaining = abs(real) if real is not None else size
                await client.place_market_order(symbol, side, remaining, slippage=slip)
                await asyncio.sleep(3)
                after = await self._get_position_strict(client, symbol)
                if after is not None and abs(after) < 1e-8:
                    logger.info("롤백 성공 (시도 %d)", attempt)
                    return True
            except Exception as e:
                logger.warning("롤백 시도 %d 실패: %s", attempt, e)
        logger.error("롤백 3회 모두 실패: %s %s %.6f", symbol, side, size)
        return False

    # ── 유틸리티 ──

    def _calc_spread_mtm(self, pos: Position, current_price: float) -> float:
        if pos.avg_entry_price <= 0 or current_price <= 0:
            return 0.0
        price_diff = current_price - pos.avg_entry_price
        if pos.direction == Direction.A:
            return price_diff * pos.maker_size
        else:
            return -price_diff * pos.maker_size

    async def _finalize_cycle(self, pos: Position, reason_override: Optional[str] = None):
        self.bot_state.exit_failure_count = 0
        await self._close_dust(pos)

        try:
            m_bal = await self._maker.get_balance()
            t_bal = await self._taker.get_balance()
            final_balance = m_bal.equity + t_bal.equity
        except Exception:
            final_balance = pos.entry_total_balance

        pnl = final_balance - pos.entry_total_balance
        self.bot_state.cycle_count += 1

        cycle = Cycle(
            cycle_id=self.bot_state.cycle_count,
            pair=pos.pair, direction=pos.direction.value,
            entry_balance=pos.entry_total_balance, exit_balance=final_balance,
            pnl=pnl, exit_reason=reason_override or pos.exit_reason,
            exit_time=time.time(), chunks=pos.chunks_filled,
        )
        self._save_cycle(cycle)

        emoji = "🟢" if pnl >= 0 else "🔴"
        await self._tg.send_alert(
            f"<b>[🏁 EXIT]</b> #{cycle.cycle_id} {pos.pair}\n"
            f"📡 {self._pair_label}\n"
            f"{emoji} PnL ${pnl:+,.2f} | {reason_override or pos.exit_reason}"
        )
        self.bot_state.position = None
        self._transition(State.COOLDOWN)

    async def _close_dust(self, pos: Position):
        m_sym = self._maker.map_symbol(pos.pair)
        t_sym = self._taker.map_symbol(pos.pair)

        for label, client, sym in [("Maker", self._maker, m_sym), ("Taker", self._taker, t_sym)]:
            try:
                remaining = await client.get_position_signed_size(sym)
                if abs(remaining) < 1e-8:
                    continue
                close_side = "SELL" if remaining > 0 else "BUY"
                await client.place_market_order(sym, close_side, abs(remaining), slippage=self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT)
                logger.info("Dust 청산: %s %s %.6f", label, close_side, abs(remaining))
            except Exception as e:
                logger.warning("Dust 청산 실패 (%s): %s", label, e)

    async def _positions_at_dust(self, pos: Position) -> bool:
        m_sym = self._maker.map_symbol(pos.pair)
        t_sym = self._taker.map_symbol(pos.pair)

        m_size = await self._get_position_strict(self._maker, m_sym)
        t_size = await self._get_position_strict(self._taker, t_sym)
        if m_size is None or t_size is None:
            return False

        try:
            mark = await self._maker.get_mark_price(m_sym)
        except Exception:
            mark = pos.avg_entry_price
        if mark <= 0:
            return False
        return abs(m_size) * mark < self.cfg.DUST_NOTIONAL_USD and abs(t_size) * mark < self.cfg.DUST_NOTIONAL_USD

    async def _fetch_funding_for_pair(self, pair: str) -> dict:
        try:
            m_sym = self._maker.map_symbol(pair)
            t_sym = self._taker.map_symbol(pair)
            m_fr = await self._maker.get_funding_rate(m_sym)
            t_fr = await self._taker.get_funding_rate(t_sym)
            return {pair: {"maker": m_fr, "taker": t_fr}}
        except Exception:
            return {}

    async def _recovery_check(self):
        for pair in self.cfg.PAIRS:
            m_sym = self._maker.map_symbol(pair)
            t_sym = self._taker.map_symbol(pair)

            m_size = await self._get_position_strict(self._maker, m_sym)
            t_size = await self._get_position_strict(self._taker, t_sym)
            if m_size is None or t_size is None:
                continue

            try:
                mark = await self._maker.get_mark_price(m_sym)
            except Exception:
                mark = 0
            if mark <= 0:
                continue

            m_notional = abs(m_size) * mark
            t_notional = abs(t_size) * mark

            if m_notional < self.cfg.DUST_NOTIONAL_USD and t_notional < self.cfg.DUST_NOTIONAL_USD:
                continue

            # 편측 포지션 감지 + 경고
            if m_notional >= self.cfg.DUST_NOTIONAL_USD and t_notional < self.cfg.DUST_NOTIONAL_USD:
                await self._tg.send_alert(
                    f"<b>[🚨 편측 감지]</b> {pair} Maker만 포지션 보유!\n"
                    f"📡 {self._pair_label}\n"
                    f"M: {m_size:+.6f} (${m_notional:.0f}) — 수동 확인 필요"
                )
                continue
            if t_notional >= self.cfg.DUST_NOTIONAL_USD and m_notional < self.cfg.DUST_NOTIONAL_USD:
                await self._tg.send_alert(
                    f"<b>[🚨 편측 감지]</b> {pair} Taker만 포지션 보유!\n"
                    f"📡 {self._pair_label}\n"
                    f"T: {t_size:+.6f} (${t_notional:.0f}) — 수동 확인 필요"
                )
                continue

            # 양쪽 동일 방향
            if (m_size > 0 and t_size > 0) or (m_size < 0 and t_size < 0):
                await self._tg.send_alert(f"<b>[🚨]</b> {pair} 양쪽 동일 방향!")
                continue

            direction = Direction.A if m_size > 0 else Direction.B
            try:
                m_bal = await self._maker.get_balance()
                t_bal = await self._taker.get_balance()
            except Exception:
                continue

            pos = Position(
                pair=pair, direction=direction,
                entry_balance=m_bal.equity,
                entry_total_balance=m_bal.equity + t_bal.equity,
                target_notional=m_notional,
                maker_size=abs(m_size), taker_size=abs(t_size),
                avg_entry_price=mark, chunks_filled=self.cfg.ENTRY_CHUNKS,
                entry_time=time.time(),
            )
            self.bot_state.position = pos
            self._transition(State.HOLD)
            await self._tg.send_alert(
                f"<b>[🔁 RECOVERY]</b> {pair} → HOLD\n"
                f"📡 {self._pair_label}\n"
                f"M: {abs(m_size):.6f} / T: {abs(t_size):.6f}"
            )
            return

    def _transition(self, new_state: State):
        old = self.bot_state.state
        self.bot_state.state = new_state
        logger.info("상태: %s → %s", old.value, new_state.value)
        self.bot_state.save(self._state_path)

    def _save_cycle(self, cycle: Cycle):
        with open(self._cycles_path, "a") as f:
            f.write(cycle.to_jsonl() + "\n")

    async def _shutdown(self):
        await self._maker.stop()
        await self._taker.stop()
        await self._tg.stop()
