from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

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
    def __init__(self, dates: Sequence[str]) -> None:
        self.dates = tuple(dates)
        self.loads = 0
        self.block_date = ""
        self.started = threading.Event()
        self.release = threading.Event()

    def latest(self, strategy: Strategy) -> RecommendationSnapshot | None:
        return None

    def recommendation_dates(self, strategy: Strategy) -> Sequence[str]:
        return self.dates

    def load_frozen(self, strategy: Strategy, trade_date: str) -> RecommendationSnapshot | None:
        self.loads += 1
        if trade_date == self.block_date:
            self.started.set()
            self.release.wait(timeout=2)
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


def test_published_index_coalesces_cold_reads_by_date_and_prefetches_three_strategies() -> None:
    dates = tuple(f"2026-06-{day:02d}" for day in range(30, 8, -1))
    archive = _Archive(dates)
    index = PublishedSnapshotIndex(archive)
    index.initialize()
    cold_date = dates[-1]
    archive.block_date = cold_date
    results: list[RecommendationSnapshot | None] = []

    first = threading.Thread(target=lambda: results.append(index.load_frozen(Strategy.TODAY, cold_date)))
    second = threading.Thread(target=lambda: results.append(index.load_frozen(Strategy.D25, cold_date)))
    first.start()
    archive.started.wait(timeout=2)
    second.start()
    archive.release.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert len(results) == 2
    assert all(snapshot is not None for snapshot in results)
    assert index.status()["cold_loads"] == 1
    assert index.status()["cold_coalesced"] == 1
    assert index.status()["cold_views"] == 3


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
