from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.pipeline_factory import build_pipeline
from trader.application.current_decisions import CurrentDecisionIndex
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendation_policy_codec import _freeze_policy
from trader.application.recommendations import RecommendationEngine
from trader.application.status import RuntimeState
from trader.application.tomorrow_events import TomorrowDecisionEventStream
from trader.application.tomorrow_freezing import DecisionRuntimeIdentity, TomorrowFreezeCoordinator
from trader.application.tomorrow_shadow import TomorrowCutoverGate, TomorrowCutoverPolicy
from trader.application.tomorrow_shadow_projection import (
    native_input_from_snapshot,
    project_tomorrow_snapshot,
)
from trader.application.tomorrow_shadow_runtime import (
    TomorrowShadowDependencies,
    TomorrowShadowRuntime,
)
from trader.application.tomorrow_views import TomorrowDecisionQueries, TomorrowQuoteOverlayIndex
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    FusionMode,
    RecommendationReplayInput,
    RecommendationSnapshot,
    Strategy,
)
from trader.infra.persistence.tomorrow_decision_freezes import TomorrowDecisionFreezeRepository
from trader.infra.persistence.writer import SnapshotRepository
from trader.infra.settings import load_strategy_settings

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
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SHANGHAI = ZoneInfo("Asia/Shanghai")


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
    assert first["published_strategies"] == ()
    assert all(record_count > 0 for record_count in first["record_counts"])


def test_tomorrow_v2_shadow_reaches_web_and_freeze_gate_without_history_download(
    tmp_path,
    application_feature_factory,
) -> None:
    observed_at = datetime.fromisoformat("2026-07-28T14:49:58+08:00").astimezone(SHANGHAI)
    now = datetime.fromisoformat("2026-07-28T14:50:01+08:00").astimezone(SHANGHAI)
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        application_feature_factory(code, observed_at)
        for code in ("600001", "600002", "300001", "300002", "688001", "688002")
    )
    replay = RecommendationReplayInput(
        schema_version="recommendation_replay_v4",
        algorithm_version="v16_board_scoring_v2",
        policy=_freeze_policy(policy),
        evaluated_at=observed_at,
        market_features=features,
        requested_codes=tuple(item.quote.code for item in features),
        candidate_features=features,
        reviews={},
        preselect_max_age_seconds=10.0,
        score_max_age_seconds=10.0,
        candidate_pool_size=120,
    )
    baseline = RecommendationSnapshot(
        snapshot_id="legacy:tomorrow:freeze",
        strategy=Strategy.TOMORROW,
        trade_date="2026-07-28",
        phase="final_review",
        data_version="legacy-input:freeze",
        strategy_version=policy.strategy_version,
        fusion_version=policy.fusion_version,
        fusion_mode=FusionMode.LOCAL_DEGRADED,
        published_at=now,
        recommendations=(),
        filtered_count=0,
        filter_reasons={},
        config_version="runtime:test",
        replay_input=replay,
    )
    projected = project_tomorrow_snapshot(baseline, policy, decision_sequence=0)
    baseline = replace(
        baseline,
        frozen=True,
        filter_reasons=projected.hard_filter_reason_counts,
    )
    repository = TomorrowDecisionFreezeRepository(tmp_path)
    repository.initialize()
    decisions = CurrentDecisionIndex()
    quotes = TomorrowQuoteOverlayIndex(decisions)
    events = TomorrowDecisionEventStream()
    clock = FixedClock(now)
    queries = TomorrowDecisionQueries(decisions, repository, clock, quotes=quotes)
    gate = TomorrowCutoverGate(TomorrowCutoverPolicy(minimum_samples=1, minimum_trade_days=1))
    freezer = TomorrowFreezeCoordinator(
        decisions,
        repository,
        clock,
        runtime_identity=DecisionRuntimeIdentity(
            "runtime:test",
            policy.strategy_version,
            policy.fusion_version,
        ),
    )
    runtime = TomorrowShadowRuntime(
        policy,
        TomorrowShadowDependencies(
            decisions,
            quotes,
            events,
            queries,
            freezer,
            gate,
            clock,
        ),
    )

    native_input = native_input_from_snapshot(baseline)
    assert runtime.process_native(native_input) is True, runtime.status()
    native_current = queries.current()
    assert native_current.status == "ready"
    assert native_current.frozen is False
    assert events.last_sequence() == 1

    assert runtime.process_native(native_input) is True, runtime.status()
    assert queries.current().decision_version == native_current.decision_version
    assert events.last_sequence() == 1

    assert runtime.process(baseline) is True, runtime.status()

    current = queries.current()
    historical = queries.history(now.date())
    assert current.status == "ready"
    assert current.frozen is True
    assert historical.status == "ready"
    assert events.last_sequence() == 2
    status = runtime.status()
    assert status["processed"] == 1
    assert status["native_processed"] == 1
    assert status["native_coalesced"] == 1
    assert status["native_superseded"] == 0
    assert status["baseline_fallbacks"] == 0
    assert current.decision_version == native_current.decision_version
    assert status["failed"] == 0
    assert status["cutover_gate"]["eligible"] is False
    assert status["cutover_gate"]["blockers"] == ("incomplete_trade_day",)
    assert status["cutover_gate"]["deepseek_request_delta"] == 0


def _run_shadow(runtime_dir: Path, recommendation_policy, application_feature_factory) -> dict[str, object]:
    initial = datetime.fromisoformat(TIMELINE[0])
    features = tuple(
        application_feature_factory(f"60000{index}", initial, industry="工业" if index < 4 else "银行")
        for index in range(1, 7)
    )
    repository = SnapshotRepository(runtime_dir, config_version="shadow-config-v2")
    pipeline = build_pipeline(
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

    assert pipeline._published_snapshots.latest(Strategy.LONG) is not None
    assert pipeline._published_snapshots.latest(Strategy.LONG).frozen is False
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


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        return self._value


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
    def refresh_intraday_tail(codes: Sequence[str], observed_at: datetime) -> None:
        del codes, observed_at

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
