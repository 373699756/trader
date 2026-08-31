from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.unit.application.v2_review_helpers import review
from trader.application.decisions.decision_core import UnifiedDecisionIndex
from trader.application.ports.scored import TodayNativeInput
from trader.application.recommendation.scored_v2_projection import build_scored_v2_hybrid, build_scored_v2_local
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import RecommendationAction, Strategy
from trader.infra.settings import load_strategy_settings

SHANGHAI = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2026, 7, 29)
EVALUATED_AT = datetime(2026, 7, 29, 11, 19, 40, tzinfo=SHANGHAI)
PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_today_native_input_directly_builds_local_and_parented_hybrid(application_feature_factory) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = _features(application_feature_factory)
    projection = build_scored_v2_local(_native_input(features), policy, sequence=1)
    assert projection.review_candidates
    code = projection.review_candidates[0].code
    applied = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = build_scored_v2_hybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=11, minute=20, second=0),
    )

    assert projection.local.strategy is Strategy.TODAY
    assert hybrid is not None and hybrid.strategy is Strategy.TODAY
    assert hybrid.parent_version == projection.local.version
    index = UnifiedDecisionIndex()
    assert index.publish(projection.local, expected_version=None).accepted
    assert index.publish(hybrid, expected_version=projection.local.version).accepted


def test_today_observe_phase_never_emits_executable_action(application_feature_factory) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = _features(application_feature_factory)
    projection = build_scored_v2_local(_native_input(features, phase="today_observe"), policy, sequence=1)

    assert all(item.action is not RecommendationAction.EXECUTABLE for item in projection.local.items)


def test_review_completed_at_112000_cannot_create_hybrid(application_feature_factory) -> None:
    policy = _recommendation_policy(load_strategy_settings(PROJECT_ROOT / "config" / "v2" / "strategy.json"))
    features = _features(application_feature_factory)
    projection = build_scored_v2_local(_native_input(features), policy, sequence=1)
    deadline = EVALUATED_AT.replace(hour=11, minute=20, second=0)
    deepseek_review = replace(review(projection.review_candidates[0].code, 100.0), completed_at=deadline)

    assert (
        build_scored_v2_hybrid(
            projection,
            policy,
            {deepseek_review.code: deepseek_review},
            review_deadline=deadline,
        )
        is None
    )


def _native_input(features: tuple[FeatureSnapshot, ...], *, phase: str = "today_main") -> TodayNativeInput:
    return TodayNativeInput(
        trade_date=TRADE_DATE,
        phase=phase,
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
        quote=replace(feature.quote, cross_source_verified=True, cross_source_deviation_pct=0.1),
        values={
            **feature.values,
            "turnover_shock_score": 70.0,
            "amount_shock_score": 70.0,
            "flow_confirmation_score": 70.0,
            "return_3d": 3.0,
            "return_5d": 5.0,
        },
    )


def _features(factory) -> tuple[FeatureSnapshot, ...]:
    return tuple(
        _verified_feature(factory(f"{prefix}{index:03d}", EVALUATED_AT - timedelta(seconds=10)))
        for prefix in ("600", "300", "688")
        for index in range(100)
    )
