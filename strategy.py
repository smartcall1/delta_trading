"""전략 로직 — 페어 선택, 진입 사이즈, 청산 조건"""
from __future__ import annotations

import math
from models import Direction, Position


def select_pair_and_direction(funding: dict) -> tuple[str, Direction, float]:
    """
    funding = {
        "ETH": {"maker": 0.0001, "taker": -0.0003},
        "BTC": {"maker": 0.0002, "taker": -0.0001},
    }
    Returns (pair, direction, net_8h_funding)
    """
    best_pair = None
    best_dir = Direction.A
    best_net = -999.0

    for pair, rates in funding.items():
        maker_fr = rates.get("maker", 0)
        taker_fr = rates.get("taker", 0)

        # Direction A: maker LONG + taker SHORT → maker receives, taker pays
        net_a = maker_fr - taker_fr  # positive = we earn
        # Direction B: maker SHORT + taker LONG
        net_b = taker_fr - maker_fr

        if net_a >= net_b:
            net, direction = net_a, Direction.A
        else:
            net, direction = net_b, Direction.B

        if net > best_net:
            best_net = net
            best_pair = pair
            best_dir = direction

    return best_pair, best_dir, best_net


def calc_entry_notional(equity: float, leverage: int) -> float:
    return equity * leverage * 0.9


def calc_chunk_size(target_notional: float, price: float, total_chunks: int) -> float:
    if price <= 0 or total_chunks <= 0:
        return 0.0
    return (target_notional / total_chunks) / price


def should_exit(pos: Position, current_total: float, taker_margin_pct: float,
                spread_mtm: float, funding: dict,
                cfg_max_hold_days: float = 2.0,
                cfg_margin_emergency_pct: float = 10.0,
                cfg_spread_opportunistic: float = 30.0,
                cfg_principal_buffer: float = 45.0,
                cfg_min_hold_minutes: int = 30) -> str | None:
    if pos.hold_days >= cfg_max_hold_days:
        return f"MAX_HOLD ({pos.hold_days:.1f}d)"

    if taker_margin_pct <= cfg_margin_emergency_pct:
        return f"MARGIN_EMERGENCY ({taker_margin_pct:.1f}%)"

    if pos.hold_minutes < cfg_min_hold_minutes:
        return None

    entry_total = pos.entry_total_balance
    if entry_total <= 0:
        return None

    pnl = current_total - entry_total
    if pnl >= cfg_spread_opportunistic:
        return f"OPPORTUNISTIC_PROFIT (${pnl:+.2f})"
    if pnl <= -cfg_principal_buffer:
        return f"PRINCIPAL_RECOVERY (${pnl:+.2f})"

    # 펀딩 역전 체크
    if funding and pos.pair in funding:
        rates = funding[pos.pair]
        maker_fr = rates.get("maker", 0)
        taker_fr = rates.get("taker", 0)
        if pos.direction == Direction.A:
            net_now = maker_fr - taker_fr
        else:
            net_now = taker_fr - maker_fr
        if net_now < -0.0002 and pos.hold_minutes >= cfg_min_hold_minutes * 2:
            return f"FUNDING_REVERSAL (net={net_now:.5f})"

    return None
