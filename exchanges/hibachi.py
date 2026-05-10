"""Hibachi 거래소 어댑터 — SDK 기반"""
from __future__ import annotations

import logging
from typing import Optional

from exchanges.base import BaseExchange, Balance, OrderResult

logger = logging.getLogger(__name__)


class HibachiExchange(BaseExchange):
    name = "hibachi"

    def __init__(
        self,
        api_key: str,
        private_key: str,
        public_key: str,
        account_id: str,
        symbol_map: Optional[dict] = None,
        **kwargs,
    ):
        self._symbol_map = symbol_map or {}
        self._client = None
        self._init_kwargs = dict(
            api_key=api_key,
            private_key=private_key,
            public_key=public_key,
            account_id=account_id,
        )

    def _ensure_client(self):
        if self._client is None:
            from exchanges._hibachi_raw import HibachiClient
            self._client = HibachiClient(**self._init_kwargs)

    def map_symbol(self, pair: str) -> str:
        return self._symbol_map.get(pair, pair)

    async def start(self) -> None:
        self._ensure_client()

    async def stop(self) -> None:
        if self._client:
            await self._client.close()

    async def get_bbo(self, symbol: str) -> dict:
        return await self._client.get_bbo(symbol)

    async def get_mark_price(self, symbol: str) -> float:
        return await self._client.get_mark_price(symbol)

    async def get_funding_rate(self, symbol: str) -> float:
        return await self._client.get_funding_rate(symbol)

    async def get_balance(self) -> Balance:
        raw = await self._client.get_balance()
        equity = float(raw.get("balance", raw.get("equity", 0)))
        return Balance(equity=equity, available_margin=equity)

    async def get_position_signed_size(self, symbol: str) -> float:
        return await self._client.get_position_signed_size(symbol)

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        result = await self._client.place_limit_order(symbol, side, price, size, post_only=post_only)
        oid = str(result.get("order_id", result.get("id", "unknown")))
        return OrderResult(order_id=oid, status="live")

    async def place_market_order(
        self, symbol: str, side: str, size: float, slippage: float = 0.01,
    ) -> OrderResult:
        result = await self._client.close_position(symbol, side, size, slippage_pct=slippage)
        return OrderResult(order_id="market", status="filled", filled_size=size)

    async def cancel_all_orders(self, symbol: str) -> int:
        result = await self._client.cancel_all_orders()
        return result.get("cancelled", 0) if isinstance(result, dict) else 0
