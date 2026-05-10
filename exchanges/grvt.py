"""GRVT 거래소 어댑터 — GrvtCcxtWS SDK"""
from __future__ import annotations

import logging
from typing import Optional

from exchanges.base import BaseExchange, Balance, OrderResult

logger = logging.getLogger(__name__)


class GRVTExchange(BaseExchange):
    name = "grvt"

    def __init__(
        self,
        api_key: str,
        private_key: str,
        trading_account_id: str,
        symbol_map: Optional[dict] = None,
        **kwargs,
    ):
        self._symbol_map = symbol_map or {}
        self._client = None
        self._init_kwargs = dict(
            api_key=api_key,
            private_key=private_key,
            trading_account_id=trading_account_id,
        )

    def _ensure_client(self):
        if self._client is None:
            from exchanges._grvt_raw import GrvtClient
            self._client = GrvtClient(**self._init_kwargs)

    def map_symbol(self, pair: str) -> str:
        return self._symbol_map.get(pair, pair)

    async def start(self) -> None:
        self._ensure_client()
        await self._client.connect()

    async def stop(self) -> None:
        if self._client:
            await self._client.close()

    async def get_bbo(self, symbol: str) -> dict:
        return await self._client.get_bbo(symbol)

    async def get_mark_price(self, symbol: str) -> float:
        price = await self._client.get_mark_price(symbol)
        return price if price else 0.0

    async def get_funding_rate(self, symbol: str) -> float:
        rate = await self._client.get_funding_rate(symbol)
        return rate if rate else 0.0

    async def get_balance(self) -> Balance:
        equity = await self._client.get_balance()
        return Balance(equity=equity, available_margin=equity)

    async def get_position_signed_size(self, symbol: str) -> float:
        positions = await self._client.get_positions(symbol)
        if not positions:
            return 0.0
        pos = positions[0]
        size = float(pos.get("size", 0))
        side = pos.get("side", "").upper()
        if side == "SHORT" or side == "SELL":
            return -size
        return size

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        result = await self._client.place_limit_order(
            symbol, side, size, price, post_only=post_only,
        )
        return OrderResult(
            order_id=result.order_id,
            status=result.status,
            filled_size=result.filled_size,
            filled_price=result.filled_price,
            message=result.message,
        )

    async def place_market_order(
        self, symbol: str, side: str, size: float, slippage: float = 0.01,
    ) -> OrderResult:
        success = await self._client.close_position(symbol, side, size, slippage_pct=slippage)
        status = "filled" if success else "error"
        return OrderResult(order_id="market", status=status, filled_size=size if success else 0.0)

    async def cancel_all_orders(self, symbol: str) -> int:
        success = await self._client.cancel_all_orders(symbol)
        return 1 if success else 0

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        return await self._client.set_leverage(symbol, leverage)
