"""공통 데이터 모델"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class State(Enum):
    IDLE = "IDLE"
    ANALYZE = "ANALYZE"
    ENTER = "ENTER"
    HOLD = "HOLD"
    HOLD_SUSPENDED = "HOLD_SUSPENDED"
    EXIT = "EXIT"
    COOLDOWN = "COOLDOWN"
    MANUAL_INTERVENTION = "MANUAL_INTERVENTION"


class Direction(Enum):
    A = "A"  # maker LONG, taker SHORT
    B = "B"  # maker SHORT, taker LONG


@dataclass
class Position:
    pair: str = ""
    direction: Direction = Direction.A
    entry_balance: float = 0.0
    entry_total_balance: float = 0.0
    target_notional: float = 0.0
    avg_entry_price: float = 0.0
    maker_size: float = 0.0
    taker_size: float = 0.0
    maker_side: str = ""
    taker_side: str = ""
    chunks_filled: int = 0
    entry_time: float = field(default_factory=time.time)
    exit_reason: str = ""

    @property
    def hold_minutes(self) -> float:
        return (time.time() - self.entry_time) / 60

    @property
    def hold_days(self) -> float:
        return self.hold_minutes / 1440

    def __post_init__(self):
        if not self.maker_side:
            self.maker_side = "BUY" if self.direction == Direction.A else "SELL"
        if not self.taker_side:
            self.taker_side = "SELL" if self.direction == Direction.A else "BUY"


@dataclass
class BotState:
    state: State = State.IDLE
    position: Optional[Position] = None
    cycle_count: int = 0
    exit_failure_count: int = 0
    suspended_since: Optional[float] = None
    last_manual_alert: float = 0.0

    def save(self, path: str):
        data = {
            "state": self.state.value,
            "cycle_count": self.cycle_count,
            "exit_failure_count": self.exit_failure_count,
            "suspended_since": self.suspended_since,
        }
        if self.position:
            data["position"] = asdict(self.position)
            data["position"]["direction"] = self.position.direction.value
        with open(path, "w") as f:
            json.dump(data, f, indent=2)


@dataclass
class Cycle:
    cycle_id: int
    pair: str
    direction: str
    entry_balance: float
    exit_balance: float
    pnl: float
    exit_reason: str
    exit_time: float
    chunks: int

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self))
