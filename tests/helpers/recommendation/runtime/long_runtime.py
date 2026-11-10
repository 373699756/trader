from __future__ import annotations

from trader.recommendation.domain.market.quote_freshness import DEFAULT_QUOTE_SESSION_TIMES, QuoteSessionTimes
from datetime import datetime
from itertools import count
from zoneinfo import ZoneInfo
from trader.recommendation.application.long_runtime import LongRuntime, LongRuntimeDependencies
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.publication.long_groups import LongGroupDefinition, LongWatchItemDefinition
from trader.recommendation.domain.publication.preview import PreviewContext

SHANGHAI = ZoneInfo("Asia/Shanghai")

NOW = datetime(2026, 8, 11, 10, 0, tzinfo=SHANGHAI)


class _Quotes:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[tuple[str, ...], datetime, bool]] = []
        self.contexts: list[PreviewContext | None] = []

    def refresh_long_quotes(self, codes, observed_at, *, force=False, deadline=None, preview_context=None):
        del deadline
        self.calls.append((tuple(codes), observed_at, force))
        self.contexts.append(preview_context)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _runtime(
    quotes: _Quotes,
    *,
    completed_at: datetime = NOW,
    enabled: bool = True,
    quote_max_age_seconds: float = 30.0,
    quote_sessions: QuoteSessionTimes = DEFAULT_QUOTE_SESSION_TIMES,
) -> tuple[LongRuntime, UnifiedFreezeIndex]:
    index = UnifiedFreezeIndex()
    runtime = LongRuntime(
        LongRuntimeDependencies(
            next_sequence=count(1).__next__,
            quote_max_age_seconds=quote_max_age_seconds,
            quote_sessions=quote_sessions,
            quotes=quotes,
            index=index,
            now=lambda: completed_at,
            enabled=enabled,
        ),
        config_version="runtime-current+strategy-fixture",
        watchlist_version="long-watchlist-fixture",
        items=(
            LongWatchItemDefinition("600001", "甲公司", "设备", NOW.date(), 10.0),
            LongWatchItemDefinition("600002", "乙公司", "材料", NOW.date(), 11.0),
            LongWatchItemDefinition("600003", "丙公司", "软件", NOW.date(), 12.0),
        ),
        groups=(
            LongGroupDefinition("设备组", "chokepoint", ("600001", "600002")),
            LongGroupDefinition("软件组", "future_growth", ("600003",)),
        ),
    )
    return runtime, index
