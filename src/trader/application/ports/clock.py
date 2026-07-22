"""Time and trading-calendar ports."""

from datetime import date, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class TradingCalendarPort(Protocol):
    def is_trading_day(self, day: date) -> bool: ...

    def session_distance(self, start: str, end: str) -> int | None: ...
