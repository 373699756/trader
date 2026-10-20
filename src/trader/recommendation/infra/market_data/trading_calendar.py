"""Read the startup calendar, without network, writes or publication adoption."""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from trader.download.domain.prepared_reference import PreparedReference
from trader.recommendation.application.ports.runtime import TradingCalendarUnavailableError


class ChinaTradingCalendar:
    def __init__(
        self,
        reference: Callable[[], PreparedReference | None],
        *,
        now: Callable[[], datetime],
        max_cache_age_days: int = 30,
    ) -> None:
        self._reference = reference
        self._now = now
        self._max_age = timedelta(days=max_cache_age_days)

    def open_dates(self) -> tuple[date, ...]:
        reference = self._reference()
        now = self._now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise TradingCalendarUnavailableError("published_calendar_clock_naive")
        now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        if (
            reference is None
            or reference.checked_at > now
            or now - reference.checked_at > self._max_age
            or now.date() > reference.open_dates[-1]
        ):
            raise TradingCalendarUnavailableError("published_calendar_unavailable")
        return reference.open_dates

    def is_trading_day(self, day: date) -> bool:
        dates = self.open_dates()
        if day < dates[0] or day > dates[-1]:
            raise TradingCalendarUnavailableError("published_calendar_outside_coverage")
        return day in dates

    def session_distance(self, start: str, end: str) -> int | None:
        try:
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            dates = self.open_dates()
        except (TradingCalendarUnavailableError, ValueError):
            return None
        if first > last or first < dates[0] or last > dates[-1]:
            return None
        return sum(first < day <= last for day in dates)


__all__ = ["ChinaTradingCalendar"]
