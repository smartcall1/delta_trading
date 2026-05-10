"""통합 Delta-Neutral 엔진 — maker/taker 거래소 무관 상태머신"""
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

        self._spread_exit_confirms = 0
        self._pending_exit_reason: Optional[str] = None

    # ── 라이프사이클 ──

    async def run(self):
        self.cfg.ensure_dirs(self.cfg.LOG_DIR)
        await self._maker.start()
        await self._taker.start()
        await self._tg.start()
        self._register_telegram_callbacks()

        await self._tg.send_alert(
            f"<b>[🚀 START]</b> Delta-Neutral 봇\n"
            f"Maker: {self._maker.name} | Taker: {self._taker.name}\n"
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
        if pos and s.state == State.HOLD:
            hold_h = pos.hold_minutes / 60
            lines.append(f"📊 #{s.cycle_count} HOLD · {pos.pair} {pos.direction.value} · {hold_h:.1f}h")
        else:
            lines.append(f"📊 {s.state.value}")
        lines.append("━" * 16)

        try:
            m_bal = await self._maker.get_balance()
            t_bal = await self._taker.get_balance()
            total = m_bal.equity + t_bal.equity
            lines.append(f"💰 <b>${total:,.0f}</b> (M ${m_bal.equity:,.0f} / T ${t_bal.equity:,.0f})")
        except Exception:
            lines.append("💰 잔고 조회 실패")

        if pos:
            entry_total = pos.entry_total_balance
            if entry_total > 0:
                pnl = total - entry_total
                emoji = "🟢" if pnl >= 0 else "🔴"
                lines.append(f"{emoji} PnL ${pnl:+,.2f}")

        lines.append(f"Maker: {self._maker.name} | Taker: {self._taker.name}")
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
        with open(self._cycles_path) as f:
            lines = f.readlines()[-5:]
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
        if not pos or self.bot_state.state not in (State.HOLD, State.MANUAL_INTERVENTION):
            await self._tg.send_alert("현재 상태에서는 강제 청산 불가")
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
        elif s == State.EXIT:
            await self._state_exit()
        elif s == State.COOLDOWN:
            await self._state_cooldown()
        elif s == State.MANUAL_INTERVENTION:
            await self._state_manual()

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

        try:
            m_sym = self._maker.map_symbol(pair)
            bbo = await self._maker.get_bbo(m_sym)
            price = bbo["mark"]
        except Exception:
            await asyncio.sleep(5)
            return

        try:
            t_bal = await self._taker.get_balance()
            t_equity = t_bal.equity
        except Exception:
            t_equity = 0

        self.bot_state.position = Position(
            pair=pair, direction=direction,
            entry_balance=equity, entry_total_balance=equity + t_equity,
            target_notional=notional, avg_entry_price=price,
        )
        self._transition(State.ANALYZE)

    # ── ANALYZE ──

    async def _state_analyze(self):
        pos = self.bot_state.position
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

        self._transition(State.ENTER)

    # ── ENTER ──

    async def _state_enter(self):
        pos = self.bot_state.position
        logger.info("ENTER: %s %s", pos.pair, pos.direction.value)

        enter_deadline = time.time() + 60 * 60
        chunk_idx = pos.chunks_filled + 1
        concession_carry = 0.0

        while chunk_idx <= self.cfg.ENTRY_CHUNKS:
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
        try:
            real_m = await self._maker.get_position_signed_size(m_sym)
            real_t = await self._taker.get_position_signed_size(t_sym)
            m_abs, t_abs = abs(real_m), abs(real_t)

            if m_abs > 0 and t_abs > 0 and abs(m_abs - t_abs) > min(m_abs, t_abs) * 0.05:
                logger.warning("진입 후 불균형: maker=%.6f taker=%.6f (>5%%)", m_abs, t_abs)
                if m_abs > t_abs:
                    close_side = "SELL" if pos.maker_side == "BUY" else "BUY"
                    await self._maker.place_market_order(m_sym, close_side, m_abs - t_abs, slippage=self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT)
                else:
                    close_side = "SELL" if pos.taker_side == "BUY" else "BUY"
                    await self._taker.place_market_order(t_sym, close_side, t_abs - m_abs, slippage=self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT)
                await asyncio.sleep(3)
                real_m = await self._maker.get_position_signed_size(m_sym)
                real_t = await self._taker.get_position_signed_size(t_sym)
                m_abs, t_abs = abs(real_m), abs(real_t)

            pos.maker_size = m_abs
            pos.taker_size = t_abs
        except Exception as e:
            logger.warning("실측 동기화 실패: %s", e)

    # ── HOLD ──

    async def _state_hold(self):
        pos = self.bot_state.position
        await asyncio.sleep(30)

        if await self._positions_at_dust(pos):
            await self._finalize_cycle(pos, reason_override="external_clear")
            return

        try:
            m_bal = await self._maker.get_balance()
            t_bal = await self._taker.get_balance()
            current_total = m_bal.equity + t_bal.equity
        except Exception:
            return

        funding = await self._fetch_funding_for_pair(pos.pair)
        reason = should_exit(
            pos, current_total, taker_margin_pct=100.0,
            spread_mtm=0.0, funding=funding,
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
            await self._tg.send_alert(f"<b>[🔔 EXIT]</b> {pos.pair} | {reason}")
        else:
            if self._spread_exit_confirms > 0:
                self._spread_exit_confirms = 0
                self._pending_exit_reason = None

    # ── EXIT ──

    async def _state_exit(self):
        pos = self.bot_state.position
        m_sym = self._maker.map_symbol(pos.pair)

        try:
            await self._maker.cancel_all_orders(m_sym)
        except Exception:
            pass

        try:
            total_to_exit = abs(await self._maker.get_position_signed_size(m_sym))
        except Exception:
            total_to_exit = pos.maker_size
        per_chunk = total_to_exit / self.cfg.EXIT_CHUNKS if total_to_exit > 0 else 0
        exited = 0.0

        for chunk_idx in range(1, self.cfg.EXIT_CHUNKS + 1):
            if await self._positions_at_dust(pos):
                break
            try:
                actual_remain = abs(await self._maker.get_position_signed_size(m_sym))
            except Exception:
                actual_remain = total_to_exit - exited
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
            await self._tg.send_alert(
                f"<b>[⚠️ MANUAL]</b> {pos.pair} 수동 개입 필요\n"
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
            try:
                bbo = await self._maker.get_bbo(m_sym)
            except Exception:
                await asyncio.sleep(2)
                continue
            spread = bbo["ask"] - bbo["bid"]

            if pos.maker_side == "BUY":
                raw = bbo["bid"] + concession
                raw = min(raw, bbo["ask"] - tick)
            else:
                raw = bbo["ask"] - concession
                raw = max(raw, bbo["bid"] + tick)
            price = _quantize_to_tick(raw, tick)

            cid = self._maker.make_client_order_id()
            try:
                size_before = await self._maker.get_position_signed_size(m_sym)
            except Exception:
                size_before = 0.0

            try:
                await self._maker.place_limit_order(
                    m_sym, pos.maker_side, price, chunk_size,
                    reduce_only=False, post_only=True, client_order_id=cid,
                )
            except Exception:
                await asyncio.sleep(2)
                continue

            await self._maker.wait_for_fill(cid, timeout=self.cfg.MAKER_FILL_TIMEOUT_SECONDS)

            try:
                await self._maker.cancel_all_orders(m_sym)
            except Exception:
                return False, concession
            await asyncio.sleep(2)

            try:
                size_after = await self._maker.get_position_signed_size(m_sym)
            except Exception:
                size_after = size_before
            actual_filled = max(0.0, (size_after - size_before) * m_sign)

            if actual_filled < chunk_size * 0.01:
                concession += max(self.cfg.MAKER_PRICE_STEP_USD, tick)
                concession = min(concession, spread * 0.5)
                continue

            fill_price = price

            # Taker 헷지
            try:
                t_before = await self._taker.get_position_signed_size(t_sym)
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
                    t_after = await self._taker.get_position_signed_size(t_sym)
                    t_filled = max(0.0, (t_after - t_before) * t_sign)
                    if t_filled >= actual_filled * 0.9:
                        break
                if t_filled < actual_filled * 0.9:
                    raise RuntimeError(f"Taker 헷지 부족: {t_filled:.6f}/{actual_filled:.6f}")
            except Exception as e:
                logger.error("Taker 헷지 실패 → Maker 롤백: %s", e)
                rollback_side = "SELL" if pos.maker_side == "BUY" else "BUY"
                try:
                    await self._maker.place_market_order(m_sym, rollback_side, actual_filled, slippage=self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT)
                except Exception as re:
                    logger.critical("Maker 롤백 실패: %s", re)
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

            if exit_side == "BUY":
                price = _quantize_to_tick(bbo["ask"] - tick, tick)
            else:
                price = _quantize_to_tick(bbo["bid"] + tick, tick)

            cid = self._maker.make_client_order_id()
            try:
                size_before = await self._maker.get_position_signed_size(m_sym)
            except Exception:
                size_before = 0.0

            try:
                await self._maker.place_limit_order(
                    m_sym, exit_side, price, chunk_size,
                    reduce_only=True, post_only=True, client_order_id=cid,
                )
            except Exception:
                await asyncio.sleep(2)
                continue

            await self._maker.wait_for_fill(cid, timeout=self.cfg.MAKER_FILL_TIMEOUT_SECONDS)
            try:
                await self._maker.cancel_all_orders(m_sym)
            except Exception:
                return 0.0
            await asyncio.sleep(2)

            try:
                size_after = await self._maker.get_position_signed_size(m_sym)
            except Exception:
                size_after = size_before
            actual_filled = max(0.0, (size_before - size_after) * (-m_exit_sign))
            if actual_filled >= chunk_size * 0.01:
                fill_price = price
                break

        # 시장가 fallback
        if actual_filled < chunk_size * 0.01:
            try:
                size_before = await self._maker.get_position_signed_size(m_sym)
                await self._maker.place_market_order(m_sym, exit_side, chunk_size, slippage=self.cfg.EMERGENCY_CLOSE_SLIPPAGE_PCT)
                await asyncio.sleep(3)
                size_after = await self._maker.get_position_signed_size(m_sym)
                actual_filled = max(0.0, (size_before - size_after) * (-m_exit_sign))
            except Exception:
                return 0.0
            if actual_filled < chunk_size * 0.01:
                return 0.0
            try:
                fill_price = (await self._maker.get_bbo(m_sym))["mark"]
            except Exception:
                pass

        # Taker 청산 + 체결 확인
        try:
            t_tick = self.cfg.get_tick_sizes(self._taker.name).get(pos.pair, 0.1)
            slip_pct = 0.005
            t_price = fill_price * (1 + slip_pct) if t_close_side == "BUY" else fill_price * (1 - slip_pct)
            t_price = _quantize_to_tick(t_price, t_tick)
            t_before = await self._taker.get_position_signed_size(t_sym)
            await self._taker.place_limit_order(
                t_sym, t_close_side, t_price, actual_filled, reduce_only=True, post_only=False,
            )
            t_exit_sign = -1.0 if t_close_side == "SELL" else 1.0
            t_closed = 0.0
            for poll in range(3):
                await asyncio.sleep(3)
                t_after = await self._taker.get_position_signed_size(t_sym)
                t_closed = max(0.0, (t_before - t_after) * (-t_exit_sign))
                if t_closed >= actual_filled * 0.9:
                    break
            if t_closed < actual_filled * 0.9:
                logger.warning("EXIT Taker 부분체결: %.6f/%.6f", t_closed, actual_filled)
        except Exception as e:
            logger.error("EXIT Taker 청산 실패: %s", e)
            return 0.0

        pos.maker_size = max(0.0, pos.maker_size - actual_filled)
        pos.taker_size = max(0.0, pos.taker_size - actual_filled)
        logger.info("EXIT 청크 %d/%d: %.6f @ %.2f", chunk_idx, total_chunks, actual_filled, fill_price)
        return actual_filled

    # ── 유틸리티 ──

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
        try:
            m_sym = self._maker.map_symbol(pos.pair)
            t_sym = self._taker.map_symbol(pos.pair)
            m_size = await self._maker.get_position_signed_size(m_sym)
            t_size = await self._taker.get_position_signed_size(t_sym)
        except Exception:
            return False
        try:
            mark = await self._maker.get_mark_price(m_sym)
        except Exception:
            mark = pos.avg_entry_price
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
            try:
                m_size = await self._maker.get_position_signed_size(m_sym)
                t_size = await self._taker.get_position_signed_size(t_sym)
            except Exception:
                continue

            try:
                mark = await self._maker.get_mark_price(m_sym)
            except Exception:
                mark = 0
            m_notional = abs(m_size) * mark
            t_notional = abs(t_size) * mark

            if m_notional < self.cfg.DUST_NOTIONAL_USD and t_notional < self.cfg.DUST_NOTIONAL_USD:
                continue

            if m_notional >= self.cfg.DUST_NOTIONAL_USD and t_notional >= self.cfg.DUST_NOTIONAL_USD:
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
