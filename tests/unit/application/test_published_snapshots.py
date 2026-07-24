from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.application.published_snapshots import PublishedSnapshotIndex
from trader.domain.recommendation.models import FusionMode, RecommendationSnapshot, Strategy

NOW = datetime(2026, 7, 22, 14, 50, tzinfo=ZoneInfo("Asia/Shanghai"))


def _snapshot(strategy: Strategy, trade_date: str) -> RecommendationSnapshot:
    return RecommendationSnapshot(
        snapshot_id=f"{strategy.value}-{trade_date}",
        strategy=strategy,
        trade_date=trade_date,
        phase="frozen",
        data_version="data-v1",
        strategy_version="strategy-v1",
        fusion_version="fusion-v1",
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=NOW,
        recommendations=(),
        filtered_count=0,
        filter_reasons={},
        config_version="runtime-v17",
        frozen=True,
    )


class _Archive:
    def __init__(
        self,
        dates: Sequence[str] = (),
        *,
        dates_by_strategy: dict[Strategy, Sequence[str]] | None = None,
    ) -> None:
        self.dates = tuple(dates)
        self.dates_by_strategy = {
            strategy: tuple(strategy_dates) for strategy, strategy_dates in (dates_by_strategy or {}).items()
        }
        self.loads = 0

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return None

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return self.dates_by_strategy.get(strategy, self.dates)

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        self.loads += 1
        if trade_date not in self.recommendation_dates(strategy):
            return None
        return _snapshot(strategy, trade_date)

    def load_live_overlay(self, strategy: Strategy, trade_date: str):
        return None


def test_published_index_keeps_complete_resident_triplets_off_persistence() -> None:
    archive = _Archive(("2026-07-22", "2026-07-21"))
    index = PublishedSnapshotIndex(archive)

    assert index.initialize() == {"resident_dates_preloaded": 2, "historical_views_preloaded": 6}
    loads_after_initialize = archive.loads

    assert index.load_frozen(Strategy.TODAY, "2026-07-22") is not None
    assert index.load_frozen(Strategy.D25, "2026-07-21") is not None
    assert index.recommendation_dates(Strategy.TOMORROW) == ("2026-07-22", "2026-07-21")
    assert archive.loads == loads_after_initialize


def test_published_index_keeps_partial_strategy_history_queryable() -> None:
    archive = _Archive(
        dates_by_strategy={
            Strategy.TODAY: ("2026-07-21",),
            Strategy.TOMORROW: ("2026-07-20", "2026-07-17"),
            Strategy.D25: ("2026-07-20", "2026-07-17"),
        }
    )
    index = PublishedSnapshotIndex(archive)

    assert index.initialize() == {"resident_dates_preloaded": 3, "historical_views_preloaded": 5}
    assert index.recommendation_dates(Strategy.TODAY) == ("2026-07-21",)
    assert index.recommendation_dates(Strategy.TOMORROW) == ("2026-07-20", "2026-07-17")
    assert index.load_frozen(Strategy.TODAY, "2026-07-21") is not None
    assert index.load_frozen(Strategy.TOMORROW, "2026-07-20") is not None
    assert index.load_frozen(Strategy.D25, "2026-07-17") is not None
    assert index.load_frozen(Strategy.TODAY, "2026-07-20") is None


def test_published_index_rejects_dates_older_than_twenty_without_storage_reads() -> None:
    dates = tuple(f"2026-06-{day:02d}" for day in range(30, 8, -1))
    archive = _Archive(dates)
    index = PublishedSnapshotIndex(archive)
    index.initialize()
    cold_date = dates[-1]
    loads_after_initialize = archive.loads

    assert index.load_frozen(Strategy.TODAY, cold_date) is None
    assert index.load_frozen(Strategy.D25, cold_date) is None
    assert archive.loads == loads_after_initialize
    assert index.status()["resident_views"] == 60


def test_published_index_exposes_only_latest_twenty_partial_strategy_dates() -> None:
    dates = tuple(f"2026-06-{day:02d}" for day in range(30, 9, -1))
    archive = _Archive(
        dates_by_strategy={
            Strategy.TODAY: dates[:-1],
            Strategy.TOMORROW: dates,
            Strategy.D25: dates[:-1],
        }
    )
    index = PublishedSnapshotIndex(archive)
    index.initialize()
    cold_date = dates[-1]

    assert index.load_frozen(Strategy.TOMORROW, cold_date) is None
    assert index.load_frozen(Strategy.TODAY, cold_date) is None
    assert index.status()["resident_views"] == 60


def test_published_index_replaces_current_draft_without_archive_write() -> None:
    archive = _Archive(())
    index = PublishedSnapshotIndex(archive)
    first = replace(_snapshot(Strategy.LONG, "2026-07-22"), frozen=False, snapshot_id="long-1")
    second = replace(first, snapshot_id="long-2")

    index.publish(first)
    index.publish(second)

    assert index.latest(Strategy.LONG) is not None
    assert index.latest(Strategy.LONG).snapshot_id == second.snapshot_id
    assert index.status()["published"] == 2


def test_published_index_preserves_selection_diagnostics_in_delivery_views() -> None:
    diagnostics = {
        "scored_candidate_count": 220,
        "actionable_candidate_count": 2,
        "score_qualified_count": 0,
        "selection_floor": 73.0,
        "maximum_local_score": 72.72,
        "maximum_final_score": 72.72,
        "empty_reason": None,
    }
    snapshot = replace(
        _snapshot(Strategy.TOMORROW, "2026-07-22"),
        metadata={
            "selection_diagnostics": diagnostics,
            "internal_only": {"must": "not leak"},
        },
    )
    archive = _Archive(
        dates_by_strategy={Strategy.TOMORROW: (snapshot.trade_date,)},
    )
    archive.load_frozen = lambda strategy, trade_date: (
        snapshot if strategy is Strategy.TOMORROW and trade_date == snapshot.trade_date else None
    )

    initialized = PublishedSnapshotIndex(archive)
    initialized.initialize()
    published = PublishedSnapshotIndex(_Archive(()))
    published.publish(snapshot)

    for delivery in (
        initialized.latest(Strategy.TOMORROW),
        initialized.load_frozen(Strategy.TOMORROW, snapshot.trade_date),
        published.latest(Strategy.TOMORROW),
    ):
        assert delivery is not None
        assert delivery.metadata == {"selection_diagnostics": diagnostics}


def test_published_index_does_not_replace_current_pin_with_older_frozen_history() -> None:
    archive = _Archive(())
    index = PublishedSnapshotIndex(archive)
    current = replace(_snapshot(Strategy.TOMORROW, "2026-07-22"), snapshot_id="current")

    index.publish(current)
    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
        index.publish(replace(_snapshot(strategy, "2026-07-21"), snapshot_id=f"older-{strategy.value}"))

    latest = index.latest(Strategy.TOMORROW)
    assert latest is not None
    assert latest.snapshot_id == "current"
    frozen = index.load_frozen(Strategy.TOMORROW, "2026-07-21")
    assert frozen is not None
    assert frozen.snapshot_id == "older-tomorrow"


def test_published_index_keeps_dates_descending_when_older_triplet_arrives() -> None:
    archive = _Archive(("2026-07-22",))
    index = PublishedSnapshotIndex(archive)
    index.initialize()

    for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
        assert index.publish(_snapshot(strategy, "2026-07-21")) is False

    assert index.recommendation_dates(Strategy.TODAY) == ("2026-07-22", "2026-07-21")
    assert index.latest(Strategy.TODAY) is not None
    assert index.latest(Strategy.TODAY).trade_date == "2026-07-22"


def test_published_index_keeps_same_day_frozen_pin_over_late_draft() -> None:
    archive = _Archive(())
    index = PublishedSnapshotIndex(archive)
    frozen = replace(_snapshot(Strategy.TODAY, "2026-07-22"), snapshot_id="frozen")
    late_draft = replace(frozen, snapshot_id="late-draft", frozen=False, phase="today_late")

    assert index.publish(frozen) is True
    assert index.publish(late_draft) is False

    latest = index.latest(Strategy.TODAY)
    assert latest is not None
    assert latest.snapshot_id == "frozen"
    assert latest.frozen is True
    assert index.status()["rejected_late_drafts"] == 1


def test_published_index_rejects_same_day_frozen_replacements() -> None:
    archive = _Archive(())
    index = PublishedSnapshotIndex(archive)
    frozen = replace(_snapshot(Strategy.TODAY, "2026-07-22"), snapshot_id="frozen")
    changed = replace(frozen, filtered_count=frozen.filtered_count + 1)
    replacement = replace(frozen, snapshot_id="replacement")

    assert index.publish(frozen) is True
    assert index.publish(changed) is False
    assert index.publish(replacement) is False

    assert index.latest(Strategy.TODAY) == frozen
    assert index.status()["rejected_frozen_replacements"] == 2


def test_published_index_raises_when_current_view_exceeds_p6_limit() -> None:
    index = PublishedSnapshotIndex(_Archive(()), maximum_view_bytes=1)

    with pytest.raises(ValueError, match="P6 view exceeds"):
        index.publish(_snapshot(Strategy.TODAY, "2026-07-22"))

    assert index.latest(Strategy.TODAY) is None
    assert index.status()["rejected_oversize_views"] == 1
