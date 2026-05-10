"""RiseX Perps 클라이언트 — Onchain CLOB (EIP-712 permit 기반)"""
from __future__ import annotations

import logging
import math
import time
import asyncio
from typing import Optional

import aiohttp
from eth_account import Account
from eth_account.messages import encode_structured_data

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2

MARKET_MAP = {
    "BTC": 1, "ETH": 2, "SOL": 3, "DOGE": 4, "XRP": 5,
    "SUI": 6, "PEPE": 7, "HYPE": 8, "TRUMP": 9, "LINK": 10,
    "ADA": 11, "AVAX": 12,
}


class RiseXClient:
    def __init__(
        self,
        account_address: str,
        signer_private_key: str,
        base_url: str = "https://api.rise.trade",
    ):
        self._account = account_address.lower()
        if not signer_private_key.startswith("0x"):
            signer_private_key = "0x" + signer_private_key
        self._signer_key = signer_private_key
        self._signer_account = Account.from_key(signer_private_key)
        self._signer_address = self._signer_account.address
        self._base = base_url.rstrip("/")
        self._session: Optional[aiohttp.ClientSession] = None

        self._eip712_domain: Optional[dict] = None
        self._router_address: str = ""
        self._markets: dict[int, dict] = {}

    async def start(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        domain_resp = await self._get("/v1/auth/eip712-domain")
        if isinstance(domain_resp, dict):
            d = domain_resp.get("data", domain_resp)
            self._eip712_domain = {
                "name": d.get("name", "RISEx"),
                "version": d.get("version", "1"),
                "chainId": int(d.get("chainId", d.get("chain_id", 4153))),
                "verifyingContract": d.get("verifyingContract", d.get("verifying_contract", "")),
            }
        config_resp = await self._get("/v1/system/config")
        if isinstance(config_resp, dict):
            cfg = config_resp.get("data", config_resp)
            contracts = cfg.get("contracts", cfg)
            self._router_address = contracts.get("router", "")

        markets_resp = await self._get("/v1/markets")
        if isinstance(markets_resp, dict):
            market_list = markets_resp.get("data", markets_resp)
            if isinstance(market_list, list):
                for m in market_list:
                    mid = m.get("market_id", m.get("id", 0))
                    self._markets[mid] = m
        logger.info("RiseX: %d markets, router=%s", len(self._markets), self._router_address[:10])

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _market_id(self, symbol: str) -> int:
        if symbol.isdigit():
            return int(symbol)
        mid = MARKET_MAP.get(symbol.upper())
        if mid is None:
            raise ValueError(f"Unknown RiseX market: {symbol}. Known: {list(MARKET_MAP.keys())}")
        return mid

    def _market_info(self, market_id: int) -> dict:
        return self._markets.get(market_id, {})

    def _price_to_ticks(self, market_id: int, price: float) -> int:
        info = self._market_info(market_id)
        step_price = float(info.get("step_price", info.get("tick_size", 0.01)))
        return int(round(price / step_price))

    def _size_to_steps(self, market_id: int, size: float) -> int:
        info = self._market_info(market_id)
        step_size = float(info.get("step_size", info.get("lot_size", 0.001)))
        return int(math.floor(size / step_size))

    # ── HTTP ──

    async def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.get(f"{self._base}{path}", params=params) as resp:
                    return await resp.json()
            except Exception as e:
                logger.warning("RiseX GET %s retry %d: %s", path, attempt + 1, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        return {}

    async def _post(self, path: str, body: dict) -> dict:
        for attempt in range(MAX_RETRIES):
            try:
                async with self._session.post(f"{self._base}{path}", json=body) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error("RiseX POST %s → %d: %s", path, resp.status, data)
                    return data
            except Exception as e:
                logger.warning("RiseX POST %s retry %d: %s", path, attempt + 1, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        return {"error": "max retries"}

    async def _get_nonce(self) -> tuple[int, int]:
        resp = await self._get(f"/v1/nonce-state/{self._account}")
        data = resp.get("data", resp)
        anchor = int(data.get("nonce_anchor", 0))
        idx = int(data.get("current_bitmap_index", 0))
        return anchor, idx

    def _build_permit(self, action_hash: bytes, nonce_anchor: int, nonce_idx: int) -> dict:
        deadline = int(time.time()) + 300

        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "VerifyWitness": [
                    {"name": "account", "type": "address"},
                    {"name": "target", "type": "address"},
                    {"name": "hash", "type": "bytes32"},
                    {"name": "nonceAnchor", "type": "uint48"},
                    {"name": "nonceBitmap", "type": "uint8"},
                    {"name": "deadline", "type": "uint32"},
                ],
            },
            "primaryType": "VerifyWitness",
            "domain": self._eip712_domain,
            "message": {
                "account": self._account,
                "target": self._router_address,
                "hash": action_hash,
                "nonceAnchor": nonce_anchor,
                "nonceBitmap": nonce_idx,
                "deadline": deadline,
            },
        }

        import base64
        signable = encode_structured_data(typed_data)
        signed = self._signer_account.sign_message(signable)
        sig_bytes = signed.signature
        if isinstance(sig_bytes, bytes):
            sig_b64 = base64.b64encode(sig_bytes).decode()
        else:
            sig_b64 = base64.b64encode(bytes.fromhex(hex(sig_bytes)[2:])).decode()

        return {
            "account": self._account,
            "signer": self._signer_address,
            "nonce_anchor": nonce_anchor,
            "nonce_bitmap_index": nonce_idx,
            "deadline": deadline,
            "signature": sig_b64,
        }

    async def _signed_post(self, path: str, body: dict, action_hash: bytes) -> dict:
        anchor, idx = await self._get_nonce()
        permit = self._build_permit(action_hash, anchor, idx)
        body["permit"] = permit
        return await self._post(path, body)

    # ── 마켓 데이터 ──

    async def get_markets(self) -> list:
        resp = await self._get("/v1/markets")
        data = resp.get("data", resp)
        return data if isinstance(data, list) else []

    async def get_bbo(self, symbol: str) -> dict:
        mid = self._market_id(symbol)
        resp = await self._get("/v1/orderbook", {"market_id": mid, "limit": 1})
        data = resp.get("data", resp)
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        bid = float(bids[0][0]) if bids else 0
        ask = float(asks[0][0]) if asks else 0
        mark = await self.get_mark_price(symbol)
        return {"bid": bid, "ask": ask, "mark": mark or (bid + ask) / 2}

    async def get_mark_price(self, symbol: str) -> float:
        mid = self._market_id(symbol)
        info = self._market_info(mid)
        if info and info.get("mark_price"):
            return float(info["mark_price"])
        markets = await self.get_markets()
        for m in markets:
            if m.get("market_id", m.get("id")) == mid:
                return float(m.get("mark_price", 0))
        return 0.0

    async def get_funding_rate(self, symbol: str) -> float:
        mid = self._market_id(symbol)
        markets = await self.get_markets()
        for m in markets:
            if m.get("market_id", m.get("id")) == mid:
                rate_8h = m.get("funding_rate_8h")
                if rate_8h is not None:
                    return float(rate_8h)
                rate_1h = m.get("current_funding_rate", 0)
                return float(rate_1h) * 8
        return 0.0

    # ── 계정 ──

    async def get_balance(self) -> dict:
        resp = await self._get("/v1/account/cross-margin-balance", {"account": self._account})
        data = resp.get("data", resp)
        balance = float(data.get("balance", 0))
        return {"equity": balance, "available_margin": balance}

    async def get_positions(self) -> list:
        resp = await self._get("/v1/positions", {"account": self._account})
        data = resp.get("data", resp)
        return data if isinstance(data, list) else []

    async def get_position_signed_size(self, symbol: str) -> float:
        mid = self._market_id(symbol)
        resp = await self._get("/v1/account/position", {"market_id": mid, "account": self._account})
        data = resp.get("data", resp)
        if not data or not isinstance(data, dict):
            return 0.0
        size = float(data.get("size", data.get("quantity", 0)))
        side = data.get("side", data.get("position_side", "")).upper()
        if side in ("SHORT", "1"):
            return -abs(size)
        return abs(size) if size != 0 else 0.0

    # ── 주문 ──

    async def place_limit_order(
        self, symbol: str, side: str, price: float, size: float,
        reduce_only: bool = False, post_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> dict:
        mid = self._market_id(symbol)
        price_ticks = self._price_to_ticks(mid, price)
        size_steps = self._size_to_steps(mid, size)

        from eth_hash.auto import keccak
        import eth_abi
        action_hash = keccak(eth_abi.encode(
            ["uint32", "uint8", "uint8", "int64", "uint64", "uint8", "bool", "bool"],
            [mid, 0 if side.upper() == "BUY" else 1, 1, price_ticks, size_steps, 0, post_only, reduce_only],
        ))

        body = {
            "market_id": mid,
            "side": 0 if side.upper() == "BUY" else 1,
            "order_type": 1,
            "price_ticks": price_ticks,
            "size_steps": size_steps,
            "time_in_force": 0,
            "post_only": post_only,
            "reduce_only": reduce_only,
            "stp_mode": 0,
            "ttl_units": 0,
            "client_order_id": client_order_id or "0",
            "builder_id": 0,
        }
        resp = await self._signed_post("/v1/orders/place", body, action_hash)
        logger.info("RiseX order: %s %s qty=%s price=%s → %s", side, symbol, size, price, resp)
        return resp

    async def place_market_order(self, symbol: str, side: str, size: float) -> dict:
        mid = self._market_id(symbol)
        size_steps = self._size_to_steps(mid, size)

        from eth_hash.auto import keccak
        import eth_abi
        action_hash = keccak(eth_abi.encode(
            ["uint32", "uint8", "uint8", "int64", "uint64", "uint8", "bool", "bool"],
            [mid, 0 if side.upper() == "BUY" else 1, 0, 0, size_steps, 3, False, False],
        ))

        body = {
            "market_id": mid,
            "side": 0 if side.upper() == "BUY" else 1,
            "order_type": 0,
            "price_ticks": 0,
            "size_steps": size_steps,
            "time_in_force": 3,
            "post_only": False,
            "reduce_only": False,
            "stp_mode": 0,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
        }
        return await self._signed_post("/v1/orders/place", body, action_hash)

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> dict:
        mid = self._market_id(symbol) if symbol else 0

        from eth_hash.auto import keccak
        import eth_abi
        action_hash = keccak(eth_abi.encode(["uint32"], [mid]))

        body = {"market_id": mid}
        return await self._signed_post("/v1/orders/cancel-all", body, action_hash)

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        mid = self._market_id(symbol)
        leverage_wad = str(leverage) + "000000000000000000"

        from eth_hash.auto import keccak
        import eth_abi
        action_hash = keccak(eth_abi.encode(["uint32", "uint256"], [mid, int(leverage_wad)]))

        body = {"market_id": mid, "leverage": leverage_wad}
        return await self._signed_post("/v1/account/leverage", body, action_hash)
