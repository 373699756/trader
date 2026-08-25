from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.unit.application.v2_review_helpers import review
from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.ports.scored import D25NativeInput, ScoredNativeInput, TomorrowNativeInput
from trader.application.research_audit import build_v2_committed_research_audit
from trader.application.scored_v2_projection import (
    build_scored_v2_hybrid,
    build_scored_v2_local,
)
from trader.bootstrap import _recommendation_policy
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import Strategy
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
    projection = build_scored_v2_local(_native_input(features), policy, sequence=1)
    assert projection.review_candidates
    assert all(item.name.startswith("测试") for item in projection.local.items)
    assert all(item.industry == "工业" for item in projection.local.items)
    assert all(item.quote is not None for item in projection.local.items)
    quote = projection.local.items[0].quote
    assert quote is not None
    source_quote = next(feature.quote for feature in features if feature.quote.code == projection.local.items[0].code)
    assert quote.price == source_quote.price
    assert quote.pct_change == source_quote.pct_change
    assert quote.amount == source_quote.amount
    assert quote.turnover_rate == source_quote.turnover_rate
    assert quote.market_cap == source_quote.market_cap
    assert quote.source == source_quote.source
    assert quote.source_time == source_quote.source_time
    assert quote.data_version == source_quote.data_version
    assert projection.local.selection_diagnostics is not None
    assert projection.local.selection_diagnostics.executable_threshold == 78.0
    assert projection.local.selection_diagnostics.observation_floor == 73.0
    assert all(item.setup_type is not None for item in projection.local.items)
    assert all(item.downside is not None for item in projection.local.items)
    assert all(item.research_coverage is not None for item in projection.local.items)
    audit = build_v2_committed_research_audit(projection, projection.local)
    assert audit.decision_hash == projection.local.content_hash
    assert audit.deepseek_request_delta == 0
    assert audit.shadow_mode == "control_copy"
    assert audit.production_local == audit.research_shadow
    assert audit.passed_candidates
    assert all(candidate.board and candidate.industry for candidate in audit.passed_candidates)
    assert any(candidate.production_top120 for candidate in audit.passed_candidates)
    code = projection.review_candidates[0].code
    applied = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = build_scored_v2_hybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=48),
    )

    assert hybrid is not None
    assert hybrid.parent_version == projection.local.version
    reviewed = next(item for item in hybrid.items if item.code == code)
    assert reviewed.review_outcome == "applied"
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
    projection = build_scored_v2_local(_native_input(features, D25NativeInput), policy, sequence=1)
    assert projection.review_candidates
    code = projection.review_candidates[0].code
    applied = replace(review(code, 100.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = build_scored_v2_hybrid(
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
    projection = build_scored_v2_local(_native_input(features), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=48)
    late = replace(review(code, 100.0), completed_at=deadline + timedelta(microseconds=1))

    assert (
        build_scored_v2_hybrid(
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
    projection = build_scored_v2_local(_native_input(features), policy, sequence=1)
    code = projection.review_candidates[0].code
    deadline = EVALUATED_AT.replace(hour=14, minute=48)
    boundary_result = replace(review(code, 100.0), completed_at=deadline)

    assert (
        build_scored_v2_hybrid(
            projection,
            policy,
            {code: boundary_result},
            review_deadline=deadline,
        )
        is None
    )


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
