"""StandX 거래소 어댑터 — REST + Ed25519 서명"""
from __future__ import annotations

import logging
import math
from typing import Optional

from exchanges.base import BaseExchange, Balance, OrderResult

logger = logging.getLogger(__name__)

STANDX_BASE_URL = "https://perps.standx.com"


def _quantize(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(round(price / tick) * tick, decimals)


class StandXExchange(BaseExchange):
    name = "standx"

    def __init__(
        self,
        jwt_token: str,
        private_key: str,
        base_url: str = STANDX_BASE_URL,
        symbol_map: Optional[dict] = None,
        tick_sizes: Optional[dict] = None,
        **kwargs,
    ):
        self._symbol_map = symbol_map or {}
        self._tick_sizes = tick_sizes or {}
        self._client = None
        self._init_kwargs = dict(
            base_url=base_url,
            jwt_token=jwt_token,
            private_key_hex=private_key,
        )

    def _ensure_client(self):
        if self._client is None:
            from exchanges._standx_raw import StandXClient
            self._client = StandXClient(**self._init_kwargs)

    def map_symbol(self, pair: str) -> str:
        return self._symbol_map.get(pair, pair)

    def _tick(self, pair: str) -> float:
        return self._tick_sizes.get(pair, 0.1)

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
            margin=raw.get("balance", raw["equity"]),
        )

    async def get_position_signed_size(self, symbol: str) -> float:
        return await self._client.get_position_signed_size(symbol)

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        result = await self._client.place_limit_order(
            symbol, side, price, size, reduce_only=reduce_only, post_only=post_only,
        )
        oid = str(result.get("data", {}).get("order_id", "unknown"))
        return OrderResult(order_id=oid, status="live")

    async def place_market_order(
        self, symbol: str, side: str, size: float, slippage: float = 0.01,
    ) -> OrderResult:
        pair_key = next((k for k, v in self._symbol_map.items() if v == symbol), symbol)
        tick = self._tick(pair_key)
        result = await self._client.place_market_order(symbol, side, size, slippage=slippage, tick_size=tick)
        return OrderResult(order_id="market", status="filled", filled_size=size)

    async def cancel_all_orders(self, symbol: str) -> int:
        result = await self._client.cancel_all_orders(symbol)
        return result.get("cancelled", 0)

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._client.change_leverage(symbol, leverage)
            return True
        except Exception as e:
            logger.warning("StandX set_leverage failed: %s", e)
            return False
