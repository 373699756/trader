from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.unit.v2_epoch_helpers import coverage, daily_field_values, market_field_values
from trader.application.ports.reviews import DeepSeekReviewUnavailableError
from trader.application.tomorrow_deepseek_fusion import (
    TomorrowDeepSeekFusionRequest,
    TomorrowDeepSeekFusionResult,
    TomorrowDeepSeekFusionUseCase,
)
from trader.domain.review.models import DeepSeekReview, DimensionAssessment, ReviewOutcome, RiskFact

NOW = datetime(2026, 7, 28, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Reviewer:
    def __init__(
        self,
        reviews: dict[str, DeepSeekReview] | None = None,
        *,
        error: DeepSeekReviewUnavailableError | None = None,
    ) -> None:
        self.reviews = reviews or {}
        self.error = error
        self.calls = 0
        self.candidate_codes: tuple[str, ...] = ()

    def review(self, _strategy, candidates, *, phase, deadline, contexts=None):
        self.calls += 1
        self.candidate_codes = tuple(item.quote.code for item in candidates)
        if self.error is not None:
            raise self.error
        return self.reviews

    def preheat(self, candidates, *, phase, deadline):
        raise AssertionError("preheat is not part of tomorrow fusion")

    def status(self):
        return {}

    def evidence_manifest_hash(self, candidate):
        return f"manifest:{candidate.quote.code}"


def test_no_eligible_candidates_skips_physical_review(
    recommendation_policy,
    application_feature_factory,
) -> None:
    feature = application_feature_factory("600001", NOW)
    feature = replace(feature, quote=replace(feature.quote, is_st=True))
    reader = _reader((feature,))
    reviewer = _Reviewer()
    use_case = TomorrowDeepSeekFusionUseCase(reader, reviewer, _policy(recommendation_policy))

    result = _execute(use_case, 10)

    assert reviewer.calls == 0
    assert result.hybrid_decision is None
    assert result.review_status == "deepseek_skipped_no_eligible_candidates"


def test_partial_valid_response_builds_degraded_hybrid_and_keeps_missing_local(
    recommendation_policy,
    application_feature_factory,
) -> None:
    features = tuple(application_feature_factory(f"600{index:03d}", NOW) for index in range(100))
    reader = _reader(features)
    reviewer = _Reviewer({"600000": _review("600000", 100.0)})
    use_case = TomorrowDeepSeekFusionUseCase(reader, reviewer, _policy(recommendation_policy))

    result = _execute(use_case, 20)

    assert result.hybrid_decision is not None
    by_code = {item.code: item for item in result.hybrid_decision.entries}
    assert by_code["600000"].score.fusion_applied is True
    assert by_code["600001"].score.fusion_applied is False
    assert by_code["600001"].score.final_score == by_code["600001"].score.local_score
    assert result.review_status == "deepseek_incomplete"
    assert "deepseek_incomplete" in result.hybrid_decision.degraded_reasons


def test_review_transport_failure_keeps_local_decision(
    recommendation_policy,
    application_feature_factory,
) -> None:
    features = tuple(application_feature_factory(f"600{index:03d}", NOW) for index in range(100))
    reader = _reader(features)
    reviewer = _Reviewer(error=DeepSeekReviewUnavailableError("timeout"))
    use_case = TomorrowDeepSeekFusionUseCase(reader, reviewer, _policy(recommendation_policy))

    result = _execute(use_case, 30)

    assert reviewer.calls == 1
    assert 0 < len(result.local_decision.entries) <= 100
    assert result.local_decision.evaluated_count == 100
    assert all(item.score.final_score == item.score.local_score for item in result.local_decision.entries)
    assert result.hybrid_decision is None
    assert result.review_status == "deepseek_transport_failed"


def test_code_mismatch_is_rejected_without_overwriting_local(
    recommendation_policy,
    application_feature_factory,
) -> None:
    features = tuple(application_feature_factory(f"600{index:03d}", NOW) for index in range(100))
    reader = _reader(features)
    reviewer = _Reviewer({"600999": _review("600999", 100.0)})
    use_case = TomorrowDeepSeekFusionUseCase(reader, reviewer, _policy(recommendation_policy))

    result = _execute(use_case, 40)

    assert result.hybrid_decision is None
    assert result.review_status == "deepseek_rejected_code_mismatch"


def test_manifest_mismatch_is_rejected_without_overwriting_local(
    recommendation_policy,
    application_feature_factory,
) -> None:
    features = tuple(application_feature_factory(f"600{index:03d}", NOW) for index in range(100))
    reader = _reader(features)
    stale = replace(_review("600000", 100.0), evidence_manifest_hash="stale-manifest")
    reviewer = _Reviewer({"600000": stale})
    use_case = TomorrowDeepSeekFusionUseCase(reader, reviewer, _policy(recommendation_policy))

    result = _execute(use_case, 50)

    assert result.hybrid_decision is None
    assert result.review_status == "deepseek_rejected_manifest_mismatch"


def test_provider_utc_review_time_is_normalized_to_shanghai(
    recommendation_policy,
    application_feature_factory,
) -> None:
    features = tuple(application_feature_factory(f"600{index:03d}", NOW) for index in range(100))
    reader = _reader(features)
    review = replace(_review("600000", 100.0), completed_at=NOW.astimezone(timezone.utc))
    use_case = TomorrowDeepSeekFusionUseCase(
        reader,
        _Reviewer({"600000": review}),
        _policy(recommendation_policy),
    )

    result = _execute(use_case, 60)

    assert result.hybrid_decision is not None
    applied = next(item for item in result.hybrid_decision.entries if item.code == "600000")
    assert applied.review is not None
    assert getattr(applied.review.completed_at.tzinfo, "key", None) == "Asia/Shanghai"


def test_future_review_risk_is_rejected_without_overwriting_local(
    recommendation_policy,
    application_feature_factory,
) -> None:
    features = tuple(application_feature_factory(f"600{index:03d}", NOW) for index in range(100))
    future_fact = RiskFact(
        risk_fact_id="future-risk",
        risk_code="regulatory_risk",
        severity="high",
        penalty=0.0,
        source="fixture",
        observed_at=NOW + timedelta(seconds=1),
        confidence=1.0,
        evidence_ids=("evidence-1",),
    )
    review = replace(_review("600000", 100.0), risk_facts=(future_fact,))
    use_case = TomorrowDeepSeekFusionUseCase(
        _reader(features),
        _Reviewer({"600000": review}),
        _policy(recommendation_policy),
    )

    result = _execute(use_case, 70)

    assert result.hybrid_decision is None
    assert result.review_status == "deepseek_rejected_invalid_time"


def _execute(
    use_case: TomorrowDeepSeekFusionUseCase,
    sequence: int,
) -> TomorrowDeepSeekFusionResult:
    return use_case.execute(
        TomorrowDeepSeekFusionRequest(
            evaluated_at=NOW,
            review_deadline=NOW + timedelta(minutes=8),
            max_age_seconds=60.0,
            decision_sequence=sequence,
        )
    )


def _reader(features):
    from trader.application.ports.market import MarketDataPlaneSnapshot
    from trader.domain.market.epochs import DailyFeaturePack, DailyFeatureRow, MarketEpoch

    rows = tuple(
        DailyFeatureRow(
            code=feature.quote.code,
            values=feature.values,
            history_sessions=60,
            data_as_of=NOW.date() - timedelta(days=1),
            security_master_version="master-v1",
            history_version="history-v1",
            field_values=daily_field_values(
                feature.values,
                source_time=NOW - timedelta(days=1),
                received_time=NOW,
            ),
        )
        for feature in features
    )
    daily = DailyFeaturePack(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-v2",
        calendar_version="calendar-v1",
        rows=rows,
        source_versions={"fixture": "daily-1"},
        coverage=coverage(tuple(feature.quote.code for feature in features)),
    )
    market_quotes = tuple(
        replace(
            feature.quote,
            board=feature.quote.board,
            board_source="security_master",
            board_reliability="verified",
            listing_age_sessions=100,
        )
        for feature in features
    )
    market = MarketEpoch(
        trade_date=NOW.date(),
        sequence=1,
        observed_at=NOW,
        received_at=NOW,
        config_version="runtime-v2",
        daily_feature_pack_version=daily.version,
        quotes=market_quotes,
        source_versions={"fixture": "market-1"},
        field_values={quote.code: market_field_values(quote) for quote in market_quotes},
    )
    snapshot = MarketDataPlaneSnapshot(daily, market, None, None)

    class _Reader:
        def snapshot(self):
            return snapshot

    return _Reader()


def _policy(policy):
    from trader.domain.market.models import Board
    from trader.domain.recommendation.models import Strategy

    candidate = {
        "liquidity": 0.4,
        "trend": 0.3,
        "stability": 0.2,
        "data_completeness": 0.1,
    }
    local = {
        "tail_structure": 0.2,
        "turnover_flow": 0.1,
        "trend": 0.2,
        "stability": 0.2,
        "market_state": 0.1,
        "entry_quality": 0.2,
    }
    boards = (Board.MAIN, Board.CHINEXT, Board.STAR)
    return replace(
        policy,
        board_policy_version="tomorrow-v2",
        board_candidate_weights={Strategy.TOMORROW: {board: candidate for board in boards}},
        board_local_strategy_weights={Strategy.TOMORROW: {board: local for board in boards}},
        selection=replace(
            policy.selection,
            candidate_min_score=50.0,
            minimum_board_reliability=0.85,
            review_candidate_limit=28,
            thresholds={**policy.selection.thresholds, "tomorrow": 78.0},
        ),
    )


def _review(code: str, score: float) -> DeepSeekReview:
    dimensions = {
        name: DimensionAssessment(
            name=name,
            score=score,
            confidence=1.0 if name != "industry_policy" else 0.0,
            assessment="fixture",
            evidence_ids=("evidence-1",) if name != "industry_policy" else (),
            is_unknown=name == "industry_policy",
        )
        for name in (
            "value_quality",
            "financial_health",
            "market_flow",
            "industry_policy",
            "risk_quality",
        )
    }
    return DeepSeekReview(
        code=code,
        outcome=ReviewOutcome.APPLIED,
        dimensions=dimensions,
        risk_facts=(),
        completed_at=NOW,
        evidence_manifest_hash=f"manifest:{code}",
    )
