"""Canonical cutoff shared by every recommendation publication path."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")
FINAL_RECOMMENDATION_TIME = time(14, 50)


def recommendation_is_frozen(now: datetime | None = None) -> bool:
    current = now or datetime.now(MARKET_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=MARKET_TIMEZONE)
    else:
        current = current.astimezone(MARKET_TIMEZONE)
    return current.weekday() < 5 and current.time() >= FINAL_RECOMMENDATION_TIME
