from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from trader.application.recommendations import RecommendationEngine
from trader.domain.market.factors import round_score
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    FilterAudit,
    RecommendationAction,
    Strategy,
)
from trader.domain.recommendation.strategies import score_strategy
from trader.domain.review.models import DeepSeekReview, DimensionAssessment, ReviewOutcome


def test_targeted_quotes_are_hard_filtered_again_before_review_and_scoring(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    valid = application_feature_factory("600001", now)
    newly_too_hot = application_feature_factory("600002", now)
    newly_too_hot = replace(newly_too_hot, quote=replace(newly_too_hot.quote, pct_change=8.01))
    reviewer = RecordingReviewer()

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        (valid, newly_too_hot),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="targeted-v2",
        review_port=reviewer,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert reviewer.reviewed_codes == ("600001",)
    assert [item.features.quote.code for item in snapshot.recommendations] == ["600001"]
    assert snapshot.filtered_count == 1
    assert snapshot.filter_reasons == {"main_board_too_hot": 1}
    assert snapshot.filter_details == (FilterAudit("600002", "main_board_too_hot", "<= 8.00", 8.01, "fixture", now),)


def test_structured_risk_is_filtered_before_local_scoring_and_review(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    valid = application_feature_factory("600001", now)
    risky = application_feature_factory("600002", now)
    risky = replace(risky, values={**risky.values, "pledge_risk": 1.0})
    reviewer = RecordingReviewer()

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        (valid, risky),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="structured-risk-v1",
        review_port=reviewer,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert reviewer.reviewed_codes == ("600001",)
    assert all(item.features.quote.code != "600002" for item in snapshot.recommendations)
    assert snapshot.filter_reasons == {"pledge_risk": 1}
    assert snapshot.filter_details == (FilterAudit("600002", "pledge_risk", "<= 0", 1.0, "fixture", now),)


def test_snapshot_returns_zero_recommendations_instead_of_lowering_threshold(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    low = application_feature_factory("600001", now)
    low = replace(
        low,
        quote=replace(
            low.quote,
            pct_change=-1.0,
            change_5m=0.0,
            volume_ratio=0.8,
            turnover_rate=0.5,
        ),
        values={name: (200_000_000.0 if name == "amount_median_20d" else 0.0) for name in low.values},
    )

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        (low,),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="below-threshold",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert snapshot.recommendations == ()
    diagnostics = snapshot.metadata["selection_diagnostics"]
    assert diagnostics["empty_reason"] == "score_below_observation_floor"
    assert diagnostics["selection_floor"] == 65.0
    assert diagnostics["scored_candidate_count"] == 1
    assert diagnostics["score_qualified_count"] == 0
    assert diagnostics["maximum_final_score"] < diagnostics["selection_floor"]


def test_deepseek_review_set_is_bounded_to_twenty_eight_candidates(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    candidates = tuple(
        application_feature_factory(f"600{index:03d}", now, industry=f"行业{index}") for index in range(40)
    )
    candidates = (
        *candidates[:-1],
        replace(
            candidates[-1],
            quote=replace(
                candidates[-1].quote,
                execution_restrictions=("board_classification_conflict",),
            ),
        ),
    )
    reviewer = RecordingReviewer()

    RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        candidates,
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="bounded-review-set",
        review_port=reviewer,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert len(reviewer.reviewed_codes) == 28
    assert "600039" in reviewer.reviewed_codes


def test_close_fallback_observes_local_candidates_below_observation_floor(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T15:05:00+08:00")
    policy = replace(
        recommendation_policy,
        selection=replace(
            recommendation_policy.selection,
            thresholds={**recommendation_policy.selection.thresholds, "d25": 99.0},
        ),
    )

    snapshot = RecommendationEngine(policy).build_snapshot(
        Strategy.D25,
        (application_feature_factory("600001", now),),
        now=now,
        phase="close_fallback",
        trade_date="2026-07-16",
        data_version="close-fallback-below-floor",
        review_port=None,
        review_deadline=now,
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert len(snapshot.recommendations) == 1
    recommendation = snapshot.recommendations[0]
    assert recommendation.action is RecommendationAction.OBSERVE
    assert recommendation.action_reason == "close_fallback_observe_only:below_score_threshold"
    assert "close_fallback_observation_floor_relaxed" in snapshot.degraded_reasons
    assert snapshot.metadata["close_fallback_observation_floor_relaxed"] is True


def test_snapshot_reports_deepseek_skip_when_board_reliability_blocks_all_candidates(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    unreliable = replace(
        application_feature_factory("600001", now),
        board_data_reliability=0.84,
    )

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TOMORROW,
        (unreliable,),
        now=now,
        phase="afternoon",
        trade_date="2026-07-16",
        data_version="unreliable-board",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T14:48:00+08:00"),
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert "deepseek_skipped_no_eligible_candidates" in snapshot.degraded_reasons
    assert "deepseek_incomplete" not in snapshot.degraded_reasons


def test_long_snapshot_does_not_report_deepseek_degradation(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.LONG,
        (application_feature_factory("600001", now),),
        now=now,
        phase="afternoon",
        trade_date="2026-07-16",
        data_version="long-local-only",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T14:48:00+08:00"),
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert "deepseek_skipped_no_eligible_candidates" not in snapshot.degraded_reasons
    assert "deepseek_incomplete" not in snapshot.degraded_reasons


def test_formal_and_watch_pools_have_independent_topk_capacity(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    features = []
    for index in range(18):
        feature = application_feature_factory(f"600{index:03d}", now, industry=f"行业{index}")
        if index >= 10:
            feature = replace(feature, values={**feature.values, "trend_breakdown": 1.0})
        features.append(feature)

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        tuple(features),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="split-pools-v17",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert len(snapshot.recommendations) == 18
    assert sum(item.action is RecommendationAction.EXECUTABLE for item in snapshot.recommendations) == 10
    assert sum(item.action is RecommendationAction.OBSERVE for item in snapshot.recommendations) == 8


def test_formal_and_watch_pools_apply_industry_limit_independently(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    features = []
    for index in range(6):
        feature = application_feature_factory(f"600{index:03d}", now, industry="同一行业")
        if index >= 3:
            feature = replace(feature, values={**feature.values, "trend_breakdown": 1.0})
        features.append(feature)

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        tuple(features),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="independent-industry-pools-v18",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert sum(item.action is RecommendationAction.EXECUTABLE for item in snapshot.recommendations) == 2
    assert sum(item.action is RecommendationAction.OBSERVE for item in snapshot.recommendations) == 2


def test_observe_only_snapshot_reports_why_formal_recommendations_are_empty(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(feature, values={**feature.values, "trend_breakdown": 1.0})

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TODAY,
        (feature,),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="observe-only-diagnostics",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert snapshot.recommendations[0].action is RecommendationAction.OBSERVE
    assert snapshot.metadata["selection_diagnostics"]["empty_reason"] == "risk_or_execution_blocked"


def test_local_and_hybrid_projections_have_distinct_snapshot_identity(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    engine = RecommendationEngine(recommendation_policy)
    prepared = engine.prepare_snapshot(
        Strategy.TODAY,
        (application_feature_factory("600001", now),),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="projection-identity-v18",
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
    )
    dimensions = {
        name: DimensionAssessment(name, 80.0, 1.0, "fixture")
        for name in ("value_quality", "financial_health", "market_flow", "risk_quality")
    }
    review = DeepSeekReview(
        code="600001",
        outcome=ReviewOutcome.APPLIED,
        dimensions=dimensions,
        risk_facts=(),
        completed_at=now,
    )

    local = engine.finalize_snapshot(prepared, {}, projection_stage="local")
    hybrid = engine.finalize_snapshot(
        prepared,
        {"600001": review},
        projection_stage="hybrid",
    )

    assert local.snapshot_id != hybrid.snapshot_id
    assert local.metadata["projection_stage"] == "local"
    assert hybrid.metadata["projection_stage"] == "hybrid"


def test_preselection_reports_history_warming_separately_from_hard_filter_reason(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(
        feature,
        values={**feature.values, "amount_median_20d": None},
        history_days=0,
    )

    candidates, reasons, _details = RecommendationEngine(recommendation_policy).preselect(
        (feature,),
        now=now,
        max_age_seconds=20.0,
        limit=120,
        strategies=(Strategy.TODAY,),
        trade_date="2026-07-16",
        phase="today_main",
    )

    assert candidates == ()
    assert reasons["missing_liquidity_history"] == 1
    assert reasons["history_warming"] == 1


def test_preselection_uses_receipt_freshness_before_targeted_quote_confirmation(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(
        feature,
        quote=replace(
            feature.quote,
            source_time=now - timedelta(seconds=120),
            received_time=now - timedelta(seconds=1),
        ),
    )

    candidates, reasons, _details = RecommendationEngine(recommendation_policy).preselect(
        (feature,),
        now=now,
        max_age_seconds=20.0,
        limit=120,
        strategies=(Strategy.TODAY,),
        trade_date="2026-07-16",
        phase="today_main",
    )

    assert [item.quote.code for item in candidates] == ["600001"]
    assert candidates[0].quote.source_time == now - timedelta(seconds=120)
    assert "stale_quote" not in reasons


def test_market_data_execution_restriction_downgrades_action_without_changing_score(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    unrestricted = application_feature_factory("600001", now)
    restricted = replace(
        unrestricted,
        quote=replace(unrestricted.quote, execution_restrictions=("market_data_degraded",)),
    )
    engine = RecommendationEngine(recommendation_policy)
    common = {
        "now": now,
        "phase": "today_main",
        "trade_date": "2026-07-16",
        "data_version": "market-restriction-v1",
        "review_port": None,
        "review_deadline": datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        "max_age_seconds": 20.0,
        "filtered_count": 0,
        "filter_reasons": {},
    }

    baseline = engine.build_snapshot(Strategy.TODAY, (unrestricted,), **common)
    degraded = engine.build_snapshot(Strategy.TODAY, (restricted,), **common)

    assert baseline.recommendations[0].score == degraded.recommendations[0].score
    assert degraded.recommendations[0].action.value == "observe"
    assert degraded.recommendations[0].action_reason == "market_data_observe_only:market_data_degraded"


def test_candidate_pool_limit_is_not_reported_as_hard_filtering(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    market = tuple(application_feature_factory(f"600{index:03d}", now) for index in range(1, 122))

    candidates, reasons, details = RecommendationEngine(recommendation_policy).preselect(
        market,
        now=now,
        max_age_seconds=20.0,
        limit=120,
    )

    assert len(candidates) == 120
    assert reasons == {}
    assert details == ()


@pytest.mark.parametrize(
    "missing_field",
    (
        "tail_return_30m_pct",
        "tail_return_30m",
        "tail_volume_ratio_raw",
        "tail_volume_ratio",
    ),
)
def test_tomorrow_snapshot_marks_incomplete_tail_data_as_degraded(
    recommendation_policy,
    application_feature_factory,
    missing_field: str,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(
        feature,
        values={**feature.values, missing_field: None},
        missing_fields=(*feature.missing_fields, missing_field),
    )

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        Strategy.TOMORROW,
        (feature,),
        now=now,
        phase="afternoon",
        trade_date="2026-07-16",
        data_version="missing-tail",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T14:48:00+08:00"),
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert "tomorrow_tail_data_incomplete" in snapshot.degraded_reasons


@pytest.mark.parametrize(
    ("strategy", "missing_field", "reason"),
    (
        (Strategy.D25, "pledge_risk", "d25_structured_research_incomplete"),
        (Strategy.LONG, "value_score", "long_research_incomplete"),
    ),
)
def test_long_horizon_snapshot_marks_incomplete_structured_research_as_degraded(
    recommendation_policy,
    application_feature_factory,
    strategy: Strategy,
    missing_field: str,
    reason: str,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(
        feature,
        values={**feature.values, missing_field: None},
        missing_fields=(*feature.missing_fields, missing_field),
    )

    snapshot = RecommendationEngine(recommendation_policy).build_snapshot(
        strategy,
        (feature,),
        now=now,
        phase="afternoon",
        trade_date="2026-07-16",
        data_version="missing-research",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T14:48:00+08:00"),
        max_age_seconds=30.0,
        filtered_count=0,
        filter_reasons={},
    )

    assert reason in snapshot.degraded_reasons
    assert snapshot.metadata["research_data_covered_count"] == 0
    assert "shadow_scoring" not in snapshot.metadata


def test_prepared_snapshot_owns_immutable_cross_thread_mappings(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T14:30:00+08:00")
    reasons = {"stale_quote": 1}
    targets = {"600001": 15.0}
    prepared = RecommendationEngine(recommendation_policy).prepare_snapshot(
        Strategy.LONG,
        (application_feature_factory("600001", now),),
        now=now,
        phase="afternoon",
        trade_date="2026-07-16",
        data_version="prepared-v1",
        review_deadline=datetime.fromisoformat("2026-07-16T14:48:00+08:00"),
        max_age_seconds=30.0,
        filtered_count=1,
        filter_reasons=reasons,
        target_prices=targets,
    )
    reasons["changed"] = 1
    targets["600001"] = 20.0

    assert prepared.filter_reasons == {"stale_quote": 1}
    assert prepared.target_prices == {"600001": 15.0}
    with pytest.raises(TypeError):
        prepared.filter_reasons["changed"] = 1  # type: ignore[index]
    with pytest.raises(TypeError):
        prepared.target_prices["600001"] = 20.0  # type: ignore[index]


def test_build_snapshot_uses_local_strategy_weights_override(
    recommendation_policy,
    application_feature_factory,
) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    higher_sentiment = application_feature_factory("600001", now)
    lower_sentiment = replace(
        higher_sentiment,
        quote=replace(higher_sentiment.quote, code="600002"),
        values={**higher_sentiment.values, "news_sentiment": 84.0},
    )
    higher_sentiment = replace(
        higher_sentiment,
        values={**higher_sentiment.values, "news_sentiment": 98.0},
    )

    custom_today_weights = {
        "momentum": 0.0,
        "liquidity": 0.0,
        "industry": 0.0,
        "sentiment": 1.0,
        "protection": 0.0,
    }
    policy = replace(
        recommendation_policy,
        local_strategy_weights={
            **recommendation_policy.local_strategy_weights,
            Strategy.TODAY: custom_today_weights,
        },
    )
    expected_lower = score_strategy(
        Strategy.TODAY,
        lower_sentiment,
        {Strategy.TODAY: custom_today_weights},
    )
    expected_higher = score_strategy(
        Strategy.TODAY,
        higher_sentiment,
        {Strategy.TODAY: custom_today_weights},
    )

    snapshot = RecommendationEngine(policy).build_snapshot(
        Strategy.TODAY,
        (lower_sentiment, higher_sentiment),
        now=now,
        phase="today_main",
        trade_date="2026-07-16",
        data_version="local-weights-v2",
        review_port=None,
        review_deadline=datetime.fromisoformat("2026-07-16T11:20:00+08:00"),
        max_age_seconds=20.0,
        filtered_count=0,
        filter_reasons={},
        filter_details=(),
    )

    assert snapshot.replay_input is not None
    assert snapshot.replay_input.policy.local_strategy_weights["today"] == custom_today_weights
    assert len(snapshot.recommendations) == 2
    assert [item.features.quote.code for item in snapshot.recommendations] == ["600001", "600002"]
    assert snapshot.recommendations[0].score.local_score == pytest.approx(round_score(expected_higher.base_score))
    assert snapshot.recommendations[1].score.local_score == pytest.approx(round_score(expected_lower.base_score))


class RecordingReviewer:
    def __init__(self) -> None:
        self.reviewed_codes: tuple[str, ...] = ()

    def review(
        self,
        _strategy: Strategy,
        candidates: tuple[FeatureSnapshot, ...],
        *,
        phase: str,
        deadline: datetime,
        contexts=None,
    ) -> dict[str, object]:
        del phase, deadline, contexts
        self.reviewed_codes = tuple(candidate.quote.code for candidate in candidates)
        return {}
