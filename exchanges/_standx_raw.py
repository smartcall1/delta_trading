"""StandX Perps REST + WebSocket 클라이언트 (dango_standx_bot 어댑터)"""
from __future__ import annotations

import base64
import json
import logging
import asyncio
import time
import uuid
from typing import Callable, Optional

import aiohttp
import base58
from nacl.signing import SigningKey

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2


class StandXClient:
    def __init__(self, base_url: str, jwt_token: str, private_key_hex: str):
        self.base_url = base_url.rstrip("/")
        self.jwt_token = jwt_token
        try:
            key_bytes = bytes.fromhex(private_key_hex)
        except ValueError:
            try:
                key_bytes = base58.b58decode(private_key_hex)
            except Exception:
                key_bytes = base64.b64decode(private_key_hex)
        if len(key_bytes) > 32:
            key_bytes = key_bytes[:32]
        self._signing_key = SigningKey(key_bytes)
        self._session: aiohttp.ClientSession | None = None

    async def start(self):
        await self._ensure_session()

    async def stop(self):
        await self.close()

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self.jwt_token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, message: str) -> str:
        signed = self._signing_key.sign(message.encode("utf-8"))
        return base64.b64encode(signed.signature).decode()

    async def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                await self._ensure_session()
                url = f"{self.base_url}{path}"
                async with self._session.request(method, url, params=params) as resp:
                    data = await resp.json()
                    if resp.status != 200:
                        logger.error("StandX API error: %s %s → %d %s", method, path, resp.status, data)
                    if isinstance(data, dict) and "code" in data:
                        return data
                    return {"code": 0, "data": data}
            except Exception as e:
                last_err = e
                logger.warning("StandX API 재시도 %d/%d: %s %s → %s", attempt + 1, MAX_RETRIES, method, path, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise last_err

    async def _signed_request(self, method: str, path: str, body: dict) -> dict:
        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                await self._ensure_session()
                url = f"{self.base_url}{path}"
                sig_id = str(uuid.uuid4())
                timestamp = str(int(time.time() * 1000))
                body_str = json.dumps(body, separators=(",", ":"))
                full_msg = f"v1,{sig_id},{timestamp},{body_str}"
                signature = self._sign(full_msg)
                body_sig = self._sign(body_str)
                headers = {
                    "X-Request-Sign-Version": "v1",
                    "X-Request-Id": sig_id,
                    "X-Request-Timestamp": timestamp,
                    "X-Request-Signature": signature,
                    "X-Body-Signature": body_sig,
                    "Content-Type": "application/json",
                }
                async with self._session.request(method, url, data=body_str, headers=headers) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text)
                    except json.JSONDecodeError:
                        if resp.status != 200:
                            raise Exception(f"StandX {resp.status}: {text[:200]}")
                        data = {"raw": text}
                    if resp.status != 200:
                        logger.error("StandX signed API error: %s %s → %d %s", method, path, resp.status, data)
                    if isinstance(data, dict) and "code" in data:
                        return data
                    return {"code": 0, "data": data}
            except Exception as e:
                last_err = e
                logger.warning("StandX signed API 재시도 %d/%d: %s", attempt + 1, MAX_RETRIES, e)
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(RETRY_DELAY * (attempt + 1))
        raise last_err

    # ── 엔진 인터페이스 메서드 ──

    async def get_balance(self) -> dict:
        resp = await self._request("GET", "/api/query_balance")
        data = resp.get("data", {})
        equity = float(data.get("total_balance", 0))
        available = float(data.get("available_balance", 0))
        return {"equity": equity, "balance": equity, "available_margin": available}

    async def get_mark_price(self, symbol: str) -> float:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        data = resp.get("data", {})
        return float(data.get("mark_price", 0))

    async def get_bbo(self, symbol: str) -> dict:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        data = resp.get("data", {})
        mark = float(data.get("mark_price", 0))
        bid = float(data.get("best_bid", data.get("bid_price", mark * 0.9999)))
        ask = float(data.get("best_ask", data.get("ask_price", mark * 1.0001)))
        return {"bid": bid, "ask": ask, "mark": mark}

    async def get_funding_rate(self, symbol: str) -> float:
        resp = await self._request("GET", "/api/query_symbol_market", {"symbol": symbol})
        data = resp.get("data", {})
        return float(data.get("funding_rate", 0))

    async def get_positions(self) -> list:
        resp = await self._request("GET", "/api/query_positions")
        raw = resp.get("data", [])
        return [p for p in raw if float(p.get("qty", 0)) != 0]

    async def get_position_signed_size(self, symbol: str) -> float:
        positions = await self.get_positions()
        for p in positions:
            if p.get("symbol") == symbol:
                qty = float(p.get("qty", 0))
                side = p.get("side", "").upper()
                if side == "SHORT":
                    return -qty
                return qty
        return 0.0

    async def place_limit_order(self, symbol: str, side: str, price: float, size: float,
                                reduce_only: bool = False, post_only: bool = False) -> dict:
        body = {
            "symbol": symbol,
            "side": side.lower(),
            "order_type": "limit",
            "price": str(price),
            "qty": str(size),
            "time_in_force": "gtc",
            "reduce_only": reduce_only,
        }
        if post_only:
            body["time_in_force"] = "post_only"
        resp = await self._signed_request("POST", "/api/new_order", body)
        code = resp.get("code")
        if code is not None and code != 0:
            raise Exception(f"StandX order rejected: code={code} msg={resp.get('message')}")
        logger.info("StandX 주문: %s %s qty=%s price=%s", side, symbol, size, price)
        return resp

    async def place_market_order(self, symbol: str, side: str, size: float,
                                 slippage: float = 0.01, tick_size: float = 0.1) -> dict:
        mark = await self.get_mark_price(symbol)
        if side.upper() == "BUY":
            price = mark * (1 + slippage)
        else:
            price = mark * (1 - slippage)
        decimals = max(0, -int(__import__("math").floor(__import__("math").log10(tick_size))))
        price = round(round(price / tick_size) * tick_size, decimals)
        return await self.place_limit_order(symbol, side, price, size, reduce_only=True)

    async def close_position(self, symbol: str, side: str, size: float,
                             slippage_pct: float = 0.005, tick_size: float = 0.1) -> dict:
        close_side = "SELL" if side.upper() == "BUY" else "BUY"
        mark = await self.get_mark_price(symbol)
        if close_side == "BUY":
            price = mark * (1 + slippage_pct)
        else:
            price = mark * (1 - slippage_pct)
        decimals = max(0, -int(__import__("math").floor(__import__("math").log10(tick_size))))
        price = round(round(price / tick_size) * tick_size, decimals)
        return await self.place_limit_order(symbol, close_side, price, size, reduce_only=True)

    async def cancel_order(self, order_id: str) -> dict:
        body = {"order_id": order_id}
        resp = await self._signed_request("POST", "/api/cancel_order", body)
        return resp.get("data", {})

    async def cancel_all_orders(self, symbol: str = None) -> dict:
        params = {"symbol": symbol} if symbol else {}
        try:
            resp = await self._request("GET", "/api/query_open_orders", params)
            orders = resp.get("data", [])
            if not isinstance(orders, list):
                return {"cancelled": 0}
        except Exception as e:
            logger.warning("StandX 미체결 조회 실패: %s", e)
            return {"cancelled": 0}
        cancelled = 0
        for order in orders:
            order_id = order.get("id") or order.get("order_id")
            if not order_id:
                continue
            try:
                await self.cancel_order(str(order_id))
                cancelled += 1
            except Exception as e:
                logger.warning("StandX 취소 실패 %s: %s", order_id, e)
        return {"cancelled": cancelled}

    async def change_leverage(self, symbol: str, leverage: int) -> dict:
        body = {"symbol": symbol, "leverage": leverage}
        resp = await self._signed_request("POST", "/api/change_leverage", body)
        return resp.get("data", {})


class StandXWSClient:
    def __init__(self, ws_url: str, symbol: str, on_price: Callable[[float], None]):
        self.ws_url = ws_url
        self.symbol = symbol
        self.on_price = on_price
        self._running = False
        self._ws = None

    async def connect(self):
        import websockets
        self._running = True
        retry_delay = 1
        while self._running:
            try:
                async with websockets.connect(self.ws_url, ping_interval=10) as ws:
                    self._ws = ws
                    retry_delay = 1
                    sub_msg = json.dumps({
                        "method": "SUBSCRIBE",
                        "params": [f"price@{self.symbol}"],
                    })
                    await ws.send(sub_msg)
                    logger.info("StandX WS connected: %s", self.symbol)
                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                            mark = data.get("data", {}).get("mark_price")
                            if mark is not None:
                                mark_price = float(mark)
                                if mark_price > 0:
                                    self.on_price(mark_price)
                        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
                            continue
            except Exception as e:
                if not self._running:
                    break
                logger.warning("StandX WS disconnected, retry in %ds: %s", retry_delay, e)
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()
