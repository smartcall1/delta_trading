"""Dango 거래소 어댑터 — GraphQL + EIP-712 서명"""
from __future__ import annotations

import sys
import os
import logging
from typing import Optional

from exchanges.base import BaseExchange, Balance, OrderResult

logger = logging.getLogger(__name__)

DANGO_GQL_URL = "https://api-mainnet.dango.zone/graphql"
DANGO_WS_URL = "wss://api-mainnet.dango.zone/graphql"
PERPS_CONTRACT = "0x90bc84df68d1aa59a857e04ed529e9a26edbea4f"
CHAIN_ID = "dango-1"


class DangoExchange(BaseExchange):
    name = "dango"

    def __init__(
        self,
        private_key: str,
        account_address: str,
        gql_url: str = DANGO_GQL_URL,
        ws_url: str = DANGO_WS_URL,
        perps_contract: str = PERPS_CONTRACT,
        chain_id: str = CHAIN_ID,
        symbol_map: Optional[dict] = None,
        **kwargs,
    ):
        self._symbol_map = symbol_map or {}
        self._client = None
        self._init_kwargs = dict(
            private_key=private_key,
            account_address=account_address,
            perps_contract=perps_contract,
            chain_id=chain_id,
            gql_url=gql_url,
            ws_url=ws_url,
        )

    def _ensure_client(self):
        if self._client is None:
            from exchanges._dango_raw import DangoClient
            self._client = DangoClient(**self._init_kwargs)

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
            margin=raw.get("margin", 0.0),
        )

    async def get_position_signed_size(self, symbol: str) -> float:
        return await self._client.get_position_signed_size(symbol)

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> OrderResult:
        cid = client_order_id or self._client.make_client_order_id()
        await self._client.place_limit_order(
            symbol, side, price, size,
            reduce_only=reduce_only, post_only=post_only, client_order_id=cid,
        )
        return OrderResult(order_id=cid, status="live")

    async def place_market_order(
        self, symbol: str, side: str, size: float, slippage: float = 0.01,
    ) -> OrderResult:
        result = await self._client.place_market_order(symbol, side, size, slippage=slippage)
        return OrderResult(order_id="market", status="filled", filled_size=size)

    async def cancel_all_orders(self, symbol: str) -> int:
        result = await self._client.cancel_all_orders(symbol)
        return result.get("cancelled", 0) if isinstance(result, dict) else 0

    async def wait_for_fill(self, order_id: str, timeout: float = 30.0) -> Optional[OrderResult]:
        result = await self._client.wait_for_fill(order_id, timeout=timeout)
        if result:
            return OrderResult(
                order_id=order_id, status="filled",
                filled_size=float(result.get("size", 0)),
                filled_price=float(result.get("price", 0)),
            )
        return None

    async def is_healthy(self) -> bool:
        return await self._client.is_healthy()

    def make_client_order_id(self) -> str:
        return self._client.make_client_order_id()
