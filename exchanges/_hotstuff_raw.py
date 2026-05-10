"""Hotstuff Perps 클라이언트 — Hyperliquid-style API (EIP-712 + MessagePack)"""
from __future__ import annotations

import json
import logging
import time
import asyncio
from typing import Optional

import aiohttp
import msgpack
from eth_account import Account
from eth_account.messages import encode_structured_data

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2

EIP712_DOMAIN = {
    "name": "HotstuffCore",
    "version": "1",
    "chainId": 1,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}


def _keccak256(data: bytes) -> bytes:
    from eth_hash.auto import keccak
    return keccak(data)


class HotstuffClient:
    def __init__(self, private_key: str, base_url: str = "https://api.hotstuff.trade"):
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self._key = private_key
        self._account = Account.from_key(private_key)
        self.address = self._account.address
        self._base = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None
        self._instruments: dict[str, int] = {}
        self._instrument_info: dict[str, dict] = {}

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        resp = await self._info("instruments", {"type": "perps"})
        if isinstance(resp, list):
            for inst in resp:
                name = inst.get("name", "")
                self._instruments[name] = inst.get("id", 0)
                self._instrument_info[name] = inst
        logger.info("Hotstuff: %d instruments loaded", len(self._instruments))

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _instrument_id(self, symbol: str) -> int:
        iid = self._instruments.get(symbol)
        if iid is None:
            raise ValueError(f"Unknown Hotstuff instrument: {symbol}. Known: {list(self._instruments.keys())}")
        return iid

    # ── HTTP ──

    async def _info(self, method: str, params: dict) -> dict | list:
        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.post(
                    f"{self._base}/info", json={"method": method, "params": params}
                ) as resp:
                    data = await resp.json()
                    return data
            except Exception as e:
                logger.warning("Hotstuff info retry %d: %s", attempt + 1, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        return {}

    async def _exchange(self, action: dict, tx_type: int = 0) -> dict:
        nonce = int(time.time() * 1000)
        action_with_nonce = {**action, "nonce": nonce}

        packed = msgpack.packb(action_with_nonce, use_bin_type=True)
        action_hash = _keccak256(packed)

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Agent": [
                    {"name": "source", "type": "string"},
                    {"name": "hash", "type": "bytes32"},
                    {"name": "txType", "type": "uint16"},
                ],
            },
            "primaryType": "Agent",
            "domain": EIP712_DOMAIN,
            "message": {
                "source": "Mainnet" if "testnet" not in self._base else "Testnet",
                "hash": action_hash,
                "txType": tx_type,
            },
        }

        signable = encode_structured_data(typed_data)
        signed = self._account.sign_message(signable)

        body = {
            "action": action_with_nonce,
            "nonce": nonce,
            "signature": {"r": hex(signed.r), "s": hex(signed.s), "v": signed.v},
            "vaultAddress": None,
        }

        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.post(f"{self._base}/exchange", json=body) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error("Hotstuff exchange error %d: %s", resp.status, data)
                    return data
            except Exception as e:
                logger.warning("Hotstuff exchange retry %d: %s", attempt + 1, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        return {"error": "max retries"}

    # ── 마켓 데이터 ──

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._info("ticker", {"symbol": symbol})
        return data if isinstance(data, dict) else {}

    async def get_bbo(self, symbol: str) -> dict:
        data = await self._info("bbo", {"symbol": symbol})
        if isinstance(data, dict):
            bid = float(data.get("best_bid_price", 0))
            ask = float(data.get("best_ask_price", 0))
            mark = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
            ticker = await self._info("ticker", {"symbol": symbol})
            if isinstance(ticker, dict) and ticker.get("mark_price"):
                mark = float(ticker["mark_price"])
            return {"bid": bid, "ask": ask, "mark": mark}
        return {"bid": 0, "ask": 0, "mark": 0}

    async def get_mark_price(self, symbol: str) -> float:
        ticker = await self._info("ticker", {"symbol": symbol})
        if isinstance(ticker, dict):
            return float(ticker.get("mark_price", 0))
        return 0.0

    async def get_funding_rate(self, symbol: str) -> float:
        ticker = await self._info("ticker", {"symbol": symbol})
        if isinstance(ticker, dict):
            return float(ticker.get("funding_rate", 0))
        return 0.0

    # ── 계정 ──

    async def get_balance(self) -> dict:
        data = await self._info("account_summary", {"user": self.address})
        if isinstance(data, dict):
            equity = float(data.get("total_account_equity", 0))
            available = float(data.get("available_balance", equity))
            return {"equity": equity, "available_margin": available}
        return {"equity": 0, "available_margin": 0}

    async def get_positions(self) -> list:
        data = await self._info("positions", {"user": self.address})
        if isinstance(data, list):
            return [p for p in data if float(p.get("size", 0)) != 0]
        return []

    async def get_position_signed_size(self, symbol: str) -> float:
        positions = await self.get_positions()
        for p in positions:
            if p.get("instrument") == symbol or p.get("symbol") == symbol:
                size = float(p.get("size", 0))
                side = p.get("position_side", "").upper()
                if side == "SHORT":
                    return -abs(size)
                return abs(size)
        return 0.0

    # ── 주문 ──

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> dict:
        iid = self._instrument_id(symbol)
        action = {
            "type": "placeOrder",
            "instrumentId": iid,
            "side": "b" if side.upper() == "BUY" else "s",
            "positionSide": "BOTH",
            "price": str(price),
            "size": str(size),
            "tif": "GTC",
            "isMarket": False,
            "ro": reduce_only,
            "po": post_only,
        }
        if client_order_id:
            action["cloid"] = client_order_id
        resp = await self._exchange(action, tx_type=1301)
        logger.info("Hotstuff order: %s %s qty=%s price=%s → %s", side, symbol, size, price, resp)
        return resp

    async def place_market_order(self, symbol: str, side: str, size: float) -> dict:
        iid = self._instrument_id(symbol)
        action = {
            "type": "placeOrder",
            "instrumentId": iid,
            "side": "b" if side.upper() == "BUY" else "s",
            "positionSide": "BOTH",
            "price": "0",
            "size": str(size),
            "tif": "IOC",
            "isMarket": True,
            "ro": False,
            "po": False,
        }
        return await self._exchange(action, tx_type=1301)

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> dict:
        if symbol:
            iid = self._instrument_id(symbol)
            action = {"type": "cancelByInstrument", "instrumentId": iid}
        else:
            action = {"type": "cancelAll"}
        return await self._exchange(action)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        iid = self._instrument_id(symbol)
        action = {
            "type": "updatePerpLeverage",
            "instrumentId": iid,
            "leverage": leverage,
        }
        return await self._exchange(action)
