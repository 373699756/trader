from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from trader.application.pipeline import RecommendationPipeline
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.domain.models import FeatureSnapshot, Strategy
from trader.infra.persistence.writer import SnapshotRepository

TRADE_DATE = "2026-07-16"
TIMELINE = (
    "2026-07-16T09:20:00+08:00",
    "2026-07-16T10:00:00+08:00",
    "2026-07-16T11:19:50+08:00",
    "2026-07-16T11:20:00+08:00",
    "2026-07-16T14:30:00+08:00",
    "2026-07-16T14:49:50+08:00",
    "2026-07-16T14:50:00+08:00",
    "2026-07-16T15:00:00+08:00",
)


def test_recorded_full_day_shadow_is_deterministic_and_freezes_real_repository(
    tmp_path,
    recommendation_policy,
    application_feature_factory,
) -> None:
    first = _run_shadow(tmp_path / "first", recommendation_policy, application_feature_factory)
    second = _run_shadow(tmp_path / "second", recommendation_policy, application_feature_factory)

    assert first == second
    assert first["manifests"] == (
        ("d25", TRADE_DATE, "committed"),
        ("today", TRADE_DATE, "committed"),
        ("tomorrow", TRADE_DATE, "committed"),
    )
    assert first["published_strategies"] == ("d25", "long", "today", "tomorrow")
    assert all(record_count > 0 for record_count in first["record_counts"])


def _run_shadow(runtime_dir: Path, recommendation_policy, application_feature_factory) -> dict[str, object]:
    initial = datetime.fromisoformat(TIMELINE[0])
    features = tuple(
        application_feature_factory(f"60000{index}", initial, industry="工业" if index < 4 else "银行")
        for index in range(1, 7)
    )
    repository = SnapshotRepository(runtime_dir, config_version="shadow-config-v2")
    pipeline = RecommendationPipeline(
        StaticMarketData(features),
        TradingDayCalendar(),
        None,
        repository,
        repository,
        SnapshotPublisher(history_size=32, client_queue_size=4),
        RecommendationEngine(recommendation_policy),
        RuntimeState(),
        config_version="shadow-config-v2",
        candidate_pool_size=120,
        event_queue_size=32,
        priority_queue_size=4,
        now=lambda: initial,
        long_codes=("600001", "600002"),
    )
    pipeline.initialize()

    for raw_time in TIMELINE:
        pipeline.run_once(datetime.fromisoformat(raw_time))

    assert repository.latest(Strategy.LONG) is not None
    assert repository.latest(Strategy.LONG).frozen is False
    with sqlite3.connect(runtime_dir / "runtime.sqlite3") as connection:
        manifest_rows = tuple(
            connection.execute(
                """
                SELECT strategy, recommend_date, status, record_count
                FROM frozen_snapshots
                ORDER BY strategy
                """
            )
        )
        published_strategies = tuple(
            row[0] for row in connection.execute("SELECT strategy FROM published_snapshots ORDER BY strategy")
        )
    hashes = tuple(
        (path.relative_to(runtime_dir).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in sorted(runtime_dir.rglob("*.json"))
    )
    return {
        "manifests": tuple(tuple(row[:3]) for row in manifest_rows),
        "record_counts": tuple(int(row[3]) for row in manifest_rows),
        "published_strategies": published_strategies,
        "json_hashes": hashes,
    }


class TradingDayCalendar:
    @staticmethod
    def is_trading_day(_day) -> bool:
        return True


class StaticMarketData:
    def __init__(self, features: Sequence[FeatureSnapshot]) -> None:
        self._features = tuple(features)

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del force, deadline
        return tuple(_at_time(feature, observed_at) for feature in self._features)

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        del include_intraday_tail, include_structured_research
        requested = set(codes)
        return tuple(_at_time(feature, observed_at) for feature in self._features if feature.quote.code in requested)

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        del force, deadline
        requested = set(codes)
        return tuple(_at_time(feature, observed_at) for feature in self._features if feature.quote.code in requested)

    @staticmethod
    def health() -> Mapping[str, object]:
        return {"status": "recorded-shadow"}


def _at_time(feature: FeatureSnapshot, observed_at: datetime) -> FeatureSnapshot:
    quote = replace(
        feature.quote,
        source_time=observed_at,
        received_time=observed_at,
        data_version=f"recorded:{observed_at.isoformat()}",
    )
    return replace(feature, quote=quote, observed_at=observed_at)
