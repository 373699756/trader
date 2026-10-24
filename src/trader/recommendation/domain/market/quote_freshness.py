"""Quote eligibility age, distinct from the actual age shown in diagnostics."""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")

if TYPE_CHECKING:
    from trader.recommendation.domain.publication.preview import PreviewContext


def QuoteFreshnessAgeSeconds(
    source_time: datetime, observed_at: datetime, *, preview_context: PreviewContext | None = None
) -> float:
    """Keep same-day closing quotes eligible through the end of that business day."""
    if source_time.utcoffset() is None or observed_at.utcoffset() is None:
        raise ValueError("quote freshness requires timezone-aware timestamps")
    if preview_context is not None:
        return 0.0 if preview_context.accepts_quote(source_time, observed_at) else float("inf")
    source = source_time.astimezone(_SHANGHAI)
    observed = observed_at.astimezone(_SHANGHAI)
    if source > observed or source.date() != observed.date():
        return float("inf")
    if source.date() == observed.date() and source.time() >= time(15, 0):
        return 0.0
    return (observed - source).total_seconds()


__all__ = ["QuoteFreshnessAgeSeconds"]
