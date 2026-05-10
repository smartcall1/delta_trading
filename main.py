"""Delta-Neutral 통합 봇 엔트리포인트"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from config import Config
from exchanges.factory import create_exchange
from engine import Engine
from telegram_ui import TelegramUI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(Config.LOG_DIR, "bot.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _build_exchange_kwargs(cfg: Config, role: str) -> dict:
    """role = 'maker' 또는 'taker'. 거래소 ID에 맞는 kwargs 생성."""
    exchange_id = getattr(cfg, f"{role.upper()}_EXCHANGE")
    prefix = role.upper()

    base = {
        "symbol_map": cfg.get_symbol_map(exchange_id),
        "tick_sizes": cfg.get_tick_sizes(exchange_id),
    }

    if exchange_id == "dango":
        base.update({
            "private_key": getattr(cfg, f"{prefix}_PRIVATE_KEY"),
            "account_address": getattr(cfg, f"{prefix}_ACCOUNT_ADDRESS"),
        })
    elif exchange_id == "standx":
        base.update({
            "jwt_token": getattr(cfg, f"{prefix}_JWT_TOKEN"),
            "private_key": getattr(cfg, f"{prefix}_PRIVATE_KEY"),
        })
    elif exchange_id == "hibachi":
        base.update({
            "api_key": getattr(cfg, f"{prefix}_API_KEY"),
            "private_key": getattr(cfg, f"{prefix}_PRIVATE_KEY"),
            "public_key": getattr(cfg, f"{prefix}_PUBLIC_KEY"),
            "account_id": getattr(cfg, f"{prefix}_ACCOUNT_ID"),
        })
    elif exchange_id == "grvt":
        base.update({
            "api_key": getattr(cfg, f"{prefix}_API_KEY"),
            "private_key": getattr(cfg, f"{prefix}_PRIVATE_KEY"),
            "trading_account_id": getattr(cfg, f"{prefix}_ACCOUNT_ID"),
        })
    else:
        raise ValueError(f"Unknown exchange: {exchange_id}")

    return base


async def main():
    cfg = Config()
    cfg.ensure_dirs(cfg.LOG_DIR)

    logger.info("=== Delta-Neutral Bot ===")
    logger.info("Maker: %s | Taker: %s", cfg.MAKER_EXCHANGE, cfg.TAKER_EXCHANGE)
    logger.info("Pairs: %s | Leverage: %dx", cfg.PAIRS, cfg.LEVERAGE)

    maker_kwargs = _build_exchange_kwargs(cfg, "maker")
    taker_kwargs = _build_exchange_kwargs(cfg, "taker")

    maker = create_exchange(cfg.MAKER_EXCHANGE, **maker_kwargs)
    taker = create_exchange(cfg.TAKER_EXCHANGE, **taker_kwargs)

    tg = TelegramUI(cfg.TELEGRAM_BOT_TOKEN, cfg.TELEGRAM_CHAT_ID)
    engine = Engine(maker=maker, taker=taker, tg=tg, cfg=cfg)

    loop = asyncio.get_running_loop()
    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, lambda: asyncio.create_task(engine.request_stop()))
        loop.add_signal_handler(signal.SIGINT, lambda: asyncio.create_task(engine.request_stop()))

    await engine.run()


if __name__ == "__main__":
    asyncio.run(main())
