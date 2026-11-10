"""Quote eligibility age, distinct from the actual age shown in diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")

if TYPE_CHECKING:
    from trader.recommendation.domain.publication.preview import PreviewContext


@dataclass(frozen=True)
class QuoteSessionTimes:
    morning_open: int
    morning_close: int
    afternoon_open: int
    afternoon_close: int

    def __post_init__(self) -> None:
        values = (self.morning_open, self.morning_close, self.afternoon_open, self.afternoon_close)
        if any(type(value) is not int for value in values) or not (
            0 <= self.morning_open < self.morning_close < self.afternoon_open < self.afternoon_close <= 15 * 60
        ):
            raise ValueError("quote sessions must be ordered and end no later than the formal freeze")


DEFAULT_QUOTE_SESSION_TIMES = QuoteSessionTimes(570, 690, 780, 900)


def QuoteSnapshotTime(
    observed_at: datetime, *, sessions: QuoteSessionTimes = DEFAULT_QUOTE_SESSION_TIMES,
) -> datetime | None:
    """Return the fixed same-day quote anchor outside continuous trading."""
    if observed_at.utcoffset() is None:
        raise ValueError("quote snapshot time requires a timezone-aware timestamp")
    observed = observed_at.astimezone(_SHANGHAI)
    current = observed.hour * 60 + observed.minute
    if current >= 900:
        return _at_minute(observed, 900)
    if sessions.morning_close <= current < sessions.afternoon_open:
        return _at_minute(observed, sessions.morning_close)
    if current >= sessions.afternoon_close:
        return _at_minute(observed, sessions.afternoon_close)
    return None


def QuoteFreshnessAgeSeconds(
    source_time: datetime, observed_at: datetime, *, preview_context: PreviewContext | None = None,
    sessions: QuoteSessionTimes = DEFAULT_QUOTE_SESSION_TIMES,
) -> float:
    """Exclude the lunch break from age and retain same-day closing quotes."""
    if source_time.utcoffset() is None or observed_at.utcoffset() is None:
        raise ValueError("quote freshness requires timezone-aware timestamps")
    if preview_context is not None:
        return 0.0 if preview_context.accepts_quote(source_time, observed_at) else float("inf")
    source = source_time.astimezone(_SHANGHAI)
    observed = observed_at.astimezone(_SHANGHAI)
    if source > observed or source.date() != observed.date():
        return float("inf")
    closing_minute = 900 if observed.hour >= 15 else sessions.afternoon_close
    if source.hour * 60 + source.minute >= closing_minute:
        return 0.0
    lunch_start = _at_minute(observed, sessions.morning_close)
    lunch_end = _at_minute(observed, sessions.afternoon_open)
    lunch_seconds = max(0.0, (min(observed, lunch_end) - max(source, lunch_start)).total_seconds())
    return (observed - source).total_seconds() - lunch_seconds


def _at_minute(observed: datetime, minute: int) -> datetime:
    return observed.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)


__all__ = ["QuoteFreshnessAgeSeconds", "QuoteSnapshotTime", "QuoteSessionTimes", "DEFAULT_QUOTE_SESSION_TIMES"]
