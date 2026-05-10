"""거래소 인스턴스 생성 팩토리"""
from __future__ import annotations

from exchanges.base import BaseExchange


def create_exchange(exchange_id: str, **kwargs) -> BaseExchange:
    exchange_id = exchange_id.lower().strip()

    if exchange_id == "dango":
        from exchanges.dango import DangoExchange
        return DangoExchange(**kwargs)
    elif exchange_id == "standx":
        from exchanges.standx import StandXExchange
        return StandXExchange(**kwargs)
    elif exchange_id == "hibachi":
        from exchanges.hibachi import HibachiExchange
        return HibachiExchange(**kwargs)
    elif exchange_id == "grvt":
        from exchanges.grvt import GRVTExchange
        return GRVTExchange(**kwargs)
    elif exchange_id == "hotstuff":
        from exchanges.hotstuff import HotstuffExchange
        return HotstuffExchange(**kwargs)
    elif exchange_id == "risex":
        from exchanges.risex import RiseXExchange
        return RiseXExchange(**kwargs)
    else:
        raise ValueError(f"Unknown exchange: {exchange_id}. Available: dango, standx, hibachi, grvt, hotstuff, risex")
