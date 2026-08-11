from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.unit.application.test_tomorrow_deepseek_fusion import _review
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.decision_observers import AsyncDecisionObserver
from trader.application.ports.tomorrow import D25NativeInput, ScoredNativeInput, TomorrowNativeInput
from trader.application.shutdown import ShutdownDeadline
from trader.application.tomorrow_v2_freezing import (
    TomorrowV2FreezeCoordinator,
    V2DecisionRuntimeIdentity,
)
from trader.application.tomorrow_v2_projection import (
    build_tomorrow_v2_hybrid,
    build_tomorrow_v2_local,
)
from trader.application.tomorrow_v2_runtime import TomorrowV2Runtime, TomorrowV2RuntimeDependencies
from trader.application.v2_research_trace import InMemoryV2ResearchTraceStore
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy
from trader.infra.persistence.decision_records import SQLiteDecisionRecordRepository
from trader.infra.settings import load_strategy_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 29)
EVALUATED_AT = datetime(2026, 7, 29, 14, 40, tzinfo=SHANGHAI)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_native_local_and_valid_facts_publish_one_parented_hybrid(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = build_tomorrow_v2_local(_native_input(features), policy, sequence=1)
    assert projection.review_candidates
    code = projection.review_candidates[0].code
    applied = replace(_review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = build_tomorrow_v2_hybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=48),
    )

    assert hybrid is not None
    assert hybrid.parent_version == projection.local.version
    index = UnifiedDecisionIndex()
    local_result = index.publish(projection.local, expected_version=None)
    hybrid_result = index.publish(hybrid, expected_version=projection.local.version)
    assert local_result.accepted and hybrid_result.accepted
    assert hybrid_result.event is not None
    assert hybrid_result.event.decision_version == hybrid.version
    assert index.snapshot(Strategy.TOMORROW).current == hybrid


def test_d25_native_local_and_valid_facts_publish_one_parented_hybrid(
    application_feature_factory,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = build_tomorrow_v2_local(_native_input(features, D25NativeInput), policy, sequence=1)
    assert projection.review_candidates
    code = projection.review_candidates[0].code
    applied = replace(_review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = build_tomorrow_v2_hybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=48),
    )

    assert hybrid is not None
    assert hybrid.parent_version == projection.local.version
    index = UnifiedDecisionIndex()
    local_result = index.publish(projection.local, expected_version=None)
    hybrid_result = index.publish(hybrid, expected_version=projection.local.version)
    assert local_result.accepted and hybrid_result.accepted
    assert hybrid_result.event is not None
    assert hybrid_result.event.decision_version == hybrid.version
    assert index.snapshot(Strategy.D25).current == hybrid


def test_review_completed_after_1448_cannot_create_hybrid(application_feature_factory) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = build_tomorrow_v2_local(_native_input(features), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=48)
    late = replace(_review(code, 100.0), completed_at=deadline + timedelta(microseconds=1))

    assert (
        build_tomorrow_v2_hybrid(
            projection,
            policy,
            {code: late},
            review_deadline=deadline,
        )
        is None
    )


def test_review_completed_at_1448_cannot_create_hybrid(application_feature_factory) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    projection = build_tomorrow_v2_local(_native_input(features), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=48)
    boundary_result = replace(_review(code, 100.0), completed_at=deadline)

    assert (
        build_tomorrow_v2_hybrid(
            projection,
            policy,
            {code: boundary_result},
            review_deadline=deadline,
        )
        is None
    )


def test_runtime_close_recovery_keeps_current_and_trace_on_the_formal_v2_identity(
    application_feature_factory,
    tmp_path: Path,
) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = tuple(
        _verified_feature(application_feature_factory(f"600{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for index in range(100)
    )
    native_input = _native_input(features)
    clock = _Clock(EVALUATED_AT)
    index = UnifiedDecisionIndex()
    repository = SQLiteDecisionRecordRepository(tmp_path)
    repository.initialize()
    trace = InMemoryV2ResearchTraceStore()
    observer = AsyncDecisionObserver((trace.record,), capacity=16)
    freezer = TomorrowV2FreezeCoordinator(
        index,
        repository,
        clock,
        runtime_identity=V2DecisionRuntimeIdentity(
            native_input.config_version,
            policy.strategy_version,
            policy.fusion_version,
        ),
    )
    runtime = TomorrowV2Runtime(
        policy,
        TomorrowV2RuntimeDependencies(
            _DynamicReviewer(clock),
            index,
            observer,
            freezer,
            clock,
        ),
    )
    assert runtime.start()
    try:
        assert runtime.offer_native(native_input)
        assert runtime.wait_idle(10.0)
        assert observer.wait_idle(10.0)
        before_close = index.snapshot(Strategy.TOMORROW).current
        assert before_close is not None
        clock.value = EVALUATED_AT + timedelta(minutes=1)
        stale_input = TomorrowNativeInput(
            trade_date=native_input.trade_date,
            phase="final_review",
            data_version="stale-data:v1",
            config_version=native_input.config_version,
            evaluated_at=clock.value,
            market_features=native_input.market_features,
            requested_codes=native_input.requested_codes,
            candidate_features=native_input.candidate_features,
            preselect_max_age_seconds=native_input.preselect_max_age_seconds,
            score_max_age_seconds=native_input.score_max_age_seconds,
            candidate_pool_size=native_input.candidate_pool_size,
        )
        assert runtime.offer_native(stale_input)
        assert runtime.wait_idle(10.0)
        assert index.snapshot(Strategy.TOMORROW).current == before_close
        assert runtime.status().input_rejection_count == 1
        clock.value = EVALUATED_AT.replace(hour=15, minute=0)
        close_input = TomorrowNativeInput(
            trade_date=native_input.trade_date,
            phase="close_fallback",
            data_version="official-close-data:v1",
            config_version=native_input.config_version,
            evaluated_at=clock.value,
            market_features=native_input.market_features,
            requested_codes=native_input.requested_codes,
            candidate_features=native_input.candidate_features,
            preselect_max_age_seconds=native_input.preselect_max_age_seconds,
            score_max_age_seconds=native_input.score_max_age_seconds,
            candidate_pool_size=native_input.candidate_pool_size,
        )

        assert runtime.offer_native(close_input)
        assert runtime.wait_idle(10.0)

        assert observer.wait_idle(10.0)
        record = repository.load(Strategy.TOMORROW, native_input.trade_date)
        assert record is not None and record.commit_kind == "close_fallback"
        assert record.decision.sequence == before_close.sequence
        current = index.snapshot(Strategy.TOMORROW)
        assert current.formal == record
        assert current.current == record.decision
        assert trace.get(record.decision.version) is not None
    finally:
        runtime.stop(wait=True, deadline=ShutdownDeadline.start(10.0))


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class _DynamicReviewer:
    def __init__(self, clock: _Clock) -> None:
        self._clock = clock

    def review(self, _strategy, candidates, *, phase, deadline, contexts=None):
        code = candidates[0].quote.code
        review = replace(
            _review(code, 100.0),
            completed_at=self._clock.now() + timedelta(seconds=1),
            evidence_manifest_hash=f"manifest:{code}",
        )
        return {code: review}

    def evidence_manifest_hash(self, candidate) -> str:
        return f"manifest:{candidate.quote.code}"

    def preheat(self, candidates, *, phase, deadline):
        return {}

    def status(self):
        return {}


def _native_input(
    features: tuple[FeatureSnapshot, ...],
    strategy: type[ScoredNativeInput] = TomorrowNativeInput,
) -> ScoredNativeInput:
    return strategy(
        trade_date=TRADE_DATE,
        phase="final_review",
        data_version="candidate-data:v2",
        config_version="runtime-v2+strategy-v2",
        evaluated_at=EVALUATED_AT,
        market_features=features,
        requested_codes=tuple(feature.quote.code for feature in features),
        candidate_features=features,
        preselect_max_age_seconds=30.0,
        score_max_age_seconds=30.0,
        candidate_pool_size=120,
    )


def _verified_feature(feature: FeatureSnapshot) -> FeatureSnapshot:
    return replace(
        feature,
        quote=replace(
            feature.quote,
            cross_source_verified=True,
            cross_source_deviation_pct=0.1,
        ),
    )
