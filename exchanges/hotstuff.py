"""Hotstuff 거래소 어댑터 — EIP-712 + MessagePack"""
from __future__ import annotations

import logging
from typing import Optional

from exchanges.base import BaseExchange, Balance, OrderResult

logger = logging.getLogger(__name__)


class HotstuffExchange(BaseExchange):
    name = "hotstuff"

    def __init__(
        self,
        private_key: str,
        base_url: str = "https://api.hotstuff.trade",
        symbol_map: Optional[dict] = None,
        tick_sizes: Optional[dict] = None,
        **kwargs,
    ):
        self._symbol_map = symbol_map or {}
        self._tick_sizes = tick_sizes or {}
        self._client = None
        self._init_kwargs = dict(private_key=private_key, base_url=base_url)

    def _ensure_client(self):
        if self._client is None:
            from exchanges._hotstuff_raw import HotstuffClient
            self._client = HotstuffClient(**self._init_kwargs)

    def map_symbol(self, pair: str) -> str:
        return self._symbol_map.get(pair, pair)

    async def start(self) -> None:
        self._ensure_client()
        await self._client.start()

    async def stop(self) -> None:
        if self._client:
            await self._client.stop()

    async def get_bbo(self, symbol: str) -> dict:
        return await self._client.get_bbo(symbol)

    async def get_mark_price(self, symbol: str) -> float:
        return await self._client.get_mark_price(symbol)

    async def get_funding_rate(self, symbol: str) -> float:
        return await self._client.get_funding_rate(symbol)

    async def get_balance(self) -> Balance:
        raw = await self._client.get_balance()
        return Balance(
            equity=raw["equity"],
            available_margin=raw.get("available_margin", raw["equity"]),
        )

    async def get_position_signed_size(self, symbol: str) -> float:
        return await self._client.get_position_signed_size(symbol)

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        resp = await self._client.place_limit_order(
            symbol, side, price, size,
            reduce_only=reduce_only, post_only=post_only,
            client_order_id=client_order_id,
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        if not data or resp.get("error"):
            raise RuntimeError(f"Hotstuff order failed: {resp}")
        status = data.get("status") or {}
        oid = str(status.get("oid", "unknown"))
        return OrderResult(order_id=oid, status="live")

    async def place_market_order(
        self, symbol: str, side: str, size: float, slippage: float = 0.01,
    ) -> OrderResult:
        resp = await self._client.place_market_order(symbol, side, size)
        return OrderResult(order_id="market", status="filled", filled_size=size)

    async def cancel_all_orders(self, symbol: str) -> int:
        resp = await self._client.cancel_all_orders(symbol)
        return int(resp.get("data", {}).get("orders_cancelled", 0)) if isinstance(resp, dict) else 0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._client.set_leverage(symbol, leverage)
            return True
        except Exception as e:
            logger.warning("Hotstuff set_leverage failed: %s", e)
            return False
