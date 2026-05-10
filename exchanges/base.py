"""거래소 어댑터 공통 인터페이스 (ABC)"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Balance:
    equity: float
    available_margin: float
    margin: float = 0.0


@dataclass
class OrderResult:
    order_id: str
    status: str  # "filled", "partial", "live", "cancelled", "error"
    filled_size: float = 0.0
    filled_price: float = 0.0
    message: str = ""


class BaseExchange(ABC):
    name: str = "base"

    # ── 라이프사이클 ──

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    # ── 마켓 데이터 ──

    @abstractmethod
    async def get_bbo(self, symbol: str) -> dict:
        """{"bid": float, "ask": float, "mark": float}"""
        ...

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> float:
        """8h 기준 정규화된 펀딩레이트 반환"""
        ...

    # ── 계정 상태 ──

    @abstractmethod
    async def get_balance(self) -> Balance: ...

    @abstractmethod
    async def get_position_signed_size(self, symbol: str) -> float:
        """양수=LONG, 음수=SHORT, 0=포지션 없음"""
        ...

    # ── 주문 ──

    @abstractmethod
    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult: ...

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: str, size: float,
        slippage: float = 0.01,
    ) -> OrderResult: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> int:
        """취소된 주문 수 반환"""
        ...

    # ── 선택적 (기본 구현 제공) ──

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        return True

    async def wait_for_fill(self, order_id: str, timeout: float = 30.0) -> Optional[OrderResult]:
        """기본 구현: timeout만큼 대기 (거래소별 override 권장)."""
        import asyncio
        await asyncio.sleep(min(timeout, 30.0))
        return None

    async def is_healthy(self) -> bool:
        return True

    def make_client_order_id(self) -> str:
        import uuid
        return str(uuid.uuid4())

    def map_symbol(self, pair: str) -> str:
        """pair (예: "ETH") → 거래소 내부 심볼로 변환. 서브클래스에서 오버라이드."""
        return pair
