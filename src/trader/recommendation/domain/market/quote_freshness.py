"""Quote eligibility age, distinct from the actual age shown in diagnostics."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def QuoteFreshnessAgeSeconds(source_time: datetime, observed_at: datetime) -> float:
    """Keep same-day closing quotes eligible through the end of that business day."""
    if source_time.utcoffset() is None or observed_at.utcoffset() is None:
        raise ValueError("quote freshness requires timezone-aware timestamps")
    source = source_time.astimezone(_SHANGHAI)
    observed = observed_at.astimezone(_SHANGHAI)
    if source > observed or source.date() != observed.date():
        return float("inf")
    if source.date() == observed.date() and source.time() >= time(15, 0):
        return 0.0
    return (observed - source).total_seconds()


__all__ = ["QuoteFreshnessAgeSeconds"]
