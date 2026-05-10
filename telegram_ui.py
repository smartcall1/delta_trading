"""텔레그램 UI — 알림 + 버튼 콜백"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

BTN_STATUS = "status"
BTN_FUNDING = "funding"
BTN_HISTORY = "history"
BTN_POSITIONS = "positions"
BTN_CLOSE = "close"
BTN_STOP = "stop"


class TelegramUI:
    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._session: Optional[aiohttp.ClientSession] = None
        self._callbacks: dict[str, Callable] = {}
        self._last_update_id = 0

    async def start(self):
        if self._token and self._chat_id:
            self._session = aiohttp.ClientSession()

    async def stop(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def register_callback(self, key: str, handler: Callable):
        self._callbacks[key] = handler

    async def send_alert(self, text: str):
        if not self._session or not self._token:
            logger.info("[TG] %s", text[:100])
            return
        url = f"https://api.telegram.org/bot{self._token}/sendMessage"
        payload = {"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"}
        try:
            async with self._session.post(url, json=payload) as resp:
                if resp.status != 200:
                    logger.warning("TG send failed: %d", resp.status)
        except Exception as e:
            logger.warning("TG send error: %s", e)

    async def poll_updates(self):
        if not self._session or not self._token:
            return
        url = f"https://api.telegram.org/bot{self._token}/getUpdates"
        params = {"offset": self._last_update_id + 1, "timeout": 1}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]
                    text = update.get("message", {}).get("text", "").strip().lower()
                    if text.startswith("/"):
                        cmd = text[1:].split("@")[0]
                        if cmd in self._callbacks:
                            try:
                                await self._callbacks[cmd]()
                            except Exception as e:
                                logger.warning("TG callback error: %s", e)
        except Exception:
            pass
