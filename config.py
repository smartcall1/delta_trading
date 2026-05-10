"""통합 Delta-Neutral 봇 설정"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # ── 거래소 선택 ──
    MAKER_EXCHANGE: str = os.getenv("MAKER_EXCHANGE", "dango")
    TAKER_EXCHANGE: str = os.getenv("TAKER_EXCHANGE", "standx")

    # ── Maker 거래소 credentials ──
    MAKER_PRIVATE_KEY: str = os.getenv("MAKER_PRIVATE_KEY", "")
    MAKER_ACCOUNT_ADDRESS: str = os.getenv("MAKER_ACCOUNT_ADDRESS", "")
    MAKER_API_KEY: str = os.getenv("MAKER_API_KEY", "")
    MAKER_PUBLIC_KEY: str = os.getenv("MAKER_PUBLIC_KEY", "")
    MAKER_ACCOUNT_ID: str = os.getenv("MAKER_ACCOUNT_ID", "")
    MAKER_JWT_TOKEN: str = os.getenv("MAKER_JWT_TOKEN", "")

    # ── Taker 거래소 credentials ──
    TAKER_PRIVATE_KEY: str = os.getenv("TAKER_PRIVATE_KEY", "")
    TAKER_ACCOUNT_ADDRESS: str = os.getenv("TAKER_ACCOUNT_ADDRESS", "")
    TAKER_API_KEY: str = os.getenv("TAKER_API_KEY", "")
    TAKER_PUBLIC_KEY: str = os.getenv("TAKER_PUBLIC_KEY", "")
    TAKER_ACCOUNT_ID: str = os.getenv("TAKER_ACCOUNT_ID", "")
    TAKER_JWT_TOKEN: str = os.getenv("TAKER_JWT_TOKEN", "")

    # ── 텔레그램 ──
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # ── 트레이딩 파라미터 ──
    LEVERAGE: int = int(os.getenv("LEVERAGE", "3"))
    PAIRS: list = field(default_factory=lambda: os.getenv("PAIRS", "ETH").split(","))
    ENTRY_CHUNKS: int = int(os.getenv("ENTRY_CHUNKS", "10"))
    EXIT_CHUNKS: int = int(os.getenv("EXIT_CHUNKS", "10"))

    # ── 보유 ──
    MIN_HOLD_MINUTES: int = int(os.getenv("MIN_HOLD_MINUTES", "30"))
    MAX_HOLD_DAYS: float = float(os.getenv("MAX_HOLD_DAYS", "2"))
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "5"))

    # ── 리스크 ──
    MARGIN_EMERGENCY_PCT: float = float(os.getenv("MARGIN_EMERGENCY_PCT", "10"))
    SPREAD_OPPORTUNISTIC_USD: float = float(os.getenv("SPREAD_OPPORTUNISTIC_USD", "30"))
    PRINCIPAL_BUFFER_USD: float = float(os.getenv("PRINCIPAL_BUFFER_USD", "45"))
    EMERGENCY_CLOSE_SLIPPAGE_PCT: float = float(os.getenv("EMERGENCY_CLOSE_SLIPPAGE_PCT", "0.01"))

    # ── 메이커 ──
    MAKER_RETRY_LIMIT: int = int(os.getenv("MAKER_RETRY_LIMIT", "5"))
    MAKER_FILL_TIMEOUT_SECONDS: float = float(os.getenv("MAKER_FILL_TIMEOUT_SECONDS", "30"))
    MAKER_PRICE_STEP_USD: float = float(os.getenv("MAKER_PRICE_STEP_USD", "0.5"))

    # ── EXIT ──
    MAX_EXIT_FAILURES: int = int(os.getenv("MAX_EXIT_FAILURES", "5"))
    SPREAD_EXIT_CONFIRM_COUNT: int = int(os.getenv("SPREAD_EXIT_CONFIRM_COUNT", "3"))
    DUST_NOTIONAL_USD: float = float(os.getenv("DUST_NOTIONAL_USD", "5"))

    # ── 심볼 매핑 (거래소별) ──
    # 형식: SYMBOL_MAP_DANGO=ETH:ethusd,BTC:btcusd
    # 형식: SYMBOL_MAP_STANDX=ETH:ETH-PERP,BTC:BTC-PERP
    SYMBOL_MAP_DANGO: dict = field(default_factory=lambda: _parse_symbol_map(os.getenv("SYMBOL_MAP_DANGO", "")))
    SYMBOL_MAP_STANDX: dict = field(default_factory=lambda: _parse_symbol_map(os.getenv("SYMBOL_MAP_STANDX", "")))
    SYMBOL_MAP_HIBACHI: dict = field(default_factory=lambda: _parse_symbol_map(os.getenv("SYMBOL_MAP_HIBACHI", "")))
    SYMBOL_MAP_GRVT: dict = field(default_factory=lambda: _parse_symbol_map(os.getenv("SYMBOL_MAP_GRVT", "")))

    # ── Tick size (거래소별) ──
    # 형식: TICK_SIZE_STANDX=ETH:0.01,BTC:0.1
    TICK_SIZE_DANGO: dict = field(default_factory=lambda: _parse_tick_map(os.getenv("TICK_SIZE_DANGO", "")))
    TICK_SIZE_STANDX: dict = field(default_factory=lambda: _parse_tick_map(os.getenv("TICK_SIZE_STANDX", "")))
    TICK_SIZE_HIBACHI: dict = field(default_factory=lambda: _parse_tick_map(os.getenv("TICK_SIZE_HIBACHI", "")))
    TICK_SIZE_GRVT: dict = field(default_factory=lambda: _parse_tick_map(os.getenv("TICK_SIZE_GRVT", "")))

    # ── 디렉토리 ──
    LOG_DIR: str = os.getenv("LOG_DIR", "logs")

    def get_symbol_map(self, exchange_id: str) -> dict:
        return getattr(self, f"SYMBOL_MAP_{exchange_id.upper()}", {})

    def get_tick_sizes(self, exchange_id: str) -> dict:
        return getattr(self, f"TICK_SIZE_{exchange_id.upper()}", {})

    @staticmethod
    def ensure_dirs(log_dir: str = "logs"):
        os.makedirs(log_dir, exist_ok=True)


def _parse_symbol_map(raw: str) -> dict:
    if not raw:
        return {}
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            k, v = item.split(":", 1)
            result[k.strip()] = v.strip()
    return result


def _parse_tick_map(raw: str) -> dict:
    if not raw:
        return {}
    result = {}
    for item in raw.split(","):
        item = item.strip()
        if ":" in item:
            k, v = item.split(":", 1)
            result[k.strip()] = float(v.strip())
    return result
