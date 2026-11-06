from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.freeze_coordinator import (
    FreezeRuntimeIdentity,
    ScoredFreezeCoordinator,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.domain.publication.freeze_identity import (
    CommittedFreezeRecord,
    ScoredFreeze,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.persistence.freeze_records import SQLiteFreezeRecords

SHANGHAI = ZoneInfo("Asia/Shanghai")

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@dataclass
class _Clock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _coordinator(
    index: UnifiedFreezeIndex,
    records: SQLiteFreezeRecords,
    clock: _Clock,
    strategy: Strategy = Strategy.TOMORROW,
) -> ScoredFreezeCoordinator:
    return ScoredFreezeCoordinator(
        index,
        records,
        clock,
        runtime_identity=FreezeRuntimeIdentity("config-current", "strategy-current", "fusion-current"),
        strategy=strategy,
    )


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 11, hour, minute, second, tzinfo=SHANGHAI)


def _publish(index: UnifiedFreezeIndex, value: ScoredFreeze) -> None:
    assert index.publish(value, expected_version=None).accepted


def _assert_formal_current_and_history(
    index: UnifiedFreezeIndex,
    records: SQLiteFreezeRecords,
    clock: _Clock,
    record: CommittedFreezeRecord,
) -> None:
    queries = UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), records, clock)
    for at in (_at(15, 0), _at(15, 0, 1), _at(15, 5)):
        if at < clock.value:
            continue
        clock.value = at
        current = queries.current(record.strategy)
        history = queries.history(record.strategy, record.trade_date)
        for view in (current, history):
            assert view.status == "ready"
            assert view.frozen is True
            assert view.frozen_at == record.committed_at
            assert view.freeze_kind == record.commit_kind
            assert view.freeze_version == record.freeze.version
            assert view.content_hash is None
            assert view.input_versions == record.freeze.input_versions
            assert view.draft is None
        for current_items, history_items in ((current.items, history.items), (current.top_scores, history.top_scores)):
            assert history_items == tuple(
                replace(
                    item,
                    price=None,
                    pct_change=None,
                    amount=None,
                    turnover_rate=None,
                    market_cap=None,
                    quote_source=None,
                    quote_time=None,
                    quote_status="missing",
                )
                for item in current_items
            )
            for item in current_items:
                assert item.quote_status == "freeze_anchor"
                assert item.price == item.anchor_price
                assert item.quote_time == item.anchor_time
                assert item.anchor_to_now_pct is None
        assert records.load(record.strategy, record.trade_date) == record
