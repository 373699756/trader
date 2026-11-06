from __future__ import annotations

from tests.helpers.recommendation.publication.freeze_identity import NOW
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import CandidatePlanSet
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.scored_selection import (
    ScoredCandidatePlan,
    ScoredCandidateStageCounts,
)

from dataclasses import replace
from datetime import timedelta, timezone
import pytest
from tests.helpers.shared.configuration import StrategySettingsFixture
from trader.recommendation.application.pipeline.candidate_pool.candidate_pool_service import CandidateFilteringService
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import FilterFeatureCandidates, ScoredSelectionIdentity, ScoredSelectionOptions
from trader.recommendation.application.pipeline.stage_output import StageOutput
from trader.recommendation.domain.candidate.filters import QuoteFreshnessBasis
from trader.recommendation.domain.evidence.pipeline import PipelineStage, SourceHealth, SourceHealthState
from trader.recommendation.domain.publication.preview import PreviewContext
from trader.recommendation.infra.policy_settings import _recommendation_policy


def _empty_plan(strategy: Strategy, start: int) -> ScoredCandidatePlan:
    del strategy
    codes = iter(range(start, start + 603))
    reserves = {
        board: tuple(f"{next(codes):06d}" for _ in range(201)) for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    return ScoredCandidatePlan(
        evaluations=(),
        reserves=reserves,
        population_versions={},
        stage_counts=ScoredCandidateStageCounts(0, 0, 0, 0, 0, 0, 0),
        hard_filter_reason_counts={},
        population_rejected_count=0,
        population_filter_reason_counts={},
    )


def test_physical_candidate_union_contains_complete_populations_above_360() -> None:
    plans = CandidatePlanSet(
        {
            strategy: _empty_plan(strategy, offset)
            for strategy, offset in zip(
                (Strategy.TOMORROW, Strategy.D25),
                (0, 603),
                strict=True,
            )
        },
        population_evaluated_at=NOW,
    )

    assert len(plans.physical_union()) == 2 * 3 * 201
    assert all(len(plans.strategy_codes(strategy)) == 3 * 201 for strategy in plans.plans)


def test_candidate_plan_resolves_only_the_strategies_that_own_failed_codes() -> None:
    plans = CandidatePlanSet(
        {
            strategy: _empty_plan(strategy, offset)
            for strategy, offset in zip(
                (Strategy.TOMORROW, Strategy.D25),
                (0, 603),
                strict=True,
            )
        },
        population_evaluated_at=NOW,
    )

    assert plans.strategies_for_codes({"000000"}) == frozenset({Strategy.TOMORROW})
    assert plans.strategies_for_codes({"000000", "000603"}) == frozenset({Strategy.TOMORROW, Strategy.D25})


@pytest.mark.parametrize("source_zone", (None, timezone.utc), ids=("shanghai", "utc"))
def test_discovery_receipt_basis_cannot_revalidate_a_previous_day_quote(application_feature_factory, source_zone):
    previous = NOW - timedelta(days=1)
    source_time = previous if source_zone is None else previous.astimezone(source_zone)
    feature = application_feature_factory("600001", previous)
    feature = replace(feature, quote=replace(feature.quote, source_time=source_time, received_time=source_time))
    service = CandidateFilteringService(_recommendation_policy(StrategySettingsFixture()), None)

    plans = service.plan((feature,), None, evaluated_at=NOW, data_version="previous-day-discovery")

    assert plans.physical_union() == ()
    for plan in plans.plans.values():
        assert {reason.code for reason in plan.evaluations[0].filter_reasons} == {"stale_quote"}
        assert plan.evaluations[0].features.quote.source_time == source_time
        assert plan.evaluations[0].features.quote.received_time == source_time


@pytest.mark.parametrize("source_offset,reason", (
    (-timedelta(seconds=31), "stale_quote"),
    (timedelta(seconds=1), "future_quote"),
))
def test_discovery_processing_time_cannot_repair_an_invalid_arrival(
    application_feature_factory, source_offset, reason,
):
    feature = application_feature_factory("600001", NOW)
    feature = replace(feature, quote=replace(feature.quote, source_time=NOW + source_offset))
    service = CandidateFilteringService(_recommendation_policy(StrategySettingsFixture()), None)

    plans = service.plan(
        (feature,), None, evaluated_at=NOW + timedelta(seconds=45), data_version="invalid-arrival",
    )

    assert plans.physical_union() == ()
    for plan in plans.plans.values():
        assert {item.code for item in plan.evaluations[0].filter_reasons} == {reason}


def test_final_filter_keeps_completion_freshness_and_cannot_select_receipt_basis(application_feature_factory) -> None:
    feature = application_feature_factory("600001", NOW)
    completed_at = NOW + timedelta(seconds=45)
    policy = _recommendation_policy(StrategySettingsFixture())
    identity = ScoredSelectionIdentity(NOW.date(), "final", "final")
    options = ScoredSelectionOptions(completed_at, 30.0, phase="afternoon")

    filtered = FilterFeatureCandidates((feature,), policy, options, identity)

    assert filtered.population.input_ready_count == 0
    assert {reason.code for reason in filtered.population.evaluations[feature.quote.code].filter_reasons} == {
        "stale_quote"
    }
    assert feature.quote.source_time == feature.quote.received_time == feature.observed_at == NOW
    with pytest.raises(ValueError, match="only valid for candidate discovery"):
        FilterFeatureCandidates(
            (feature,),
            policy,
            replace(options, population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT),
            identity,
        )

    refreshed_but_old = replace(feature, observed_at=completed_at, quote=replace(feature.quote, received_time=completed_at))
    plans = CandidateFilteringService(policy, None).plan(
        (feature,), (refreshed_but_old,), evaluated_at=completed_at, data_version="refreshed-but-old",
    )
    assert plans.physical_union() == ()
    assert all(
        "stale_quote" in {reason.code for item in plan.evaluations for reason in item.filter_reasons}
        for plan in plans.plans.values()
    )


@pytest.mark.parametrize("observed_pipeline", (False, True))
def test_discovery_checks_each_receipt_and_preserves_invalid_source_time(
    application_feature_factory,
    observed_pipeline: bool,
) -> None:
    first = application_feature_factory("600001", NOW)
    later = application_feature_factory("600002", NOW + timedelta(seconds=40))
    invalid = replace(first, quote=replace(first.quote, code="600003", source_time=NOW.replace(tzinfo=None)))
    features = (first, later, invalid)
    completed_at = NOW + timedelta(seconds=90)
    service = CandidateFilteringService(_recommendation_policy(StrategySettingsFixture()), None)

    if observed_pipeline:
        source = StageOutput(
            PipelineStage.DYNAMIC_MARKET,
            features,
            input_batch_id="slow-discovery",
            as_of=completed_at,
            input_count=len(features),
            source_health=SourceHealth(SourceHealthState.UNAVAILABLE, 1, 0),
            latency_ms=90_000,
        )
        plans = service.plan_observed(
            source, evaluated_at=completed_at, data_version="slow-discovery", monotonic=lambda: 0.0,
        ).plans
    else:
        plans = service.plan(features, None, evaluated_at=completed_at, data_version="slow-discovery")

    assert plans.physical_union() == (first.quote.code, later.quote.code)
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        evaluations = {item.code: item for item in plans.plans[strategy].evaluations}
        assert plans.strategy_codes(strategy) == (first.quote.code, later.quote.code)
        assert {reason.code for reason in evaluations[invalid.quote.code].filter_reasons} == {"invalid_quote_time"}
        for original in features:
            retained = evaluations[original.quote.code].features
            assert retained.quote.source_time == original.quote.source_time
            assert retained.quote.received_time == original.quote.received_time
            assert retained.observed_at == original.observed_at


@pytest.mark.parametrize("invalid_receipt", (NOW.replace(tzinfo=None), NOW + timedelta(seconds=46)))
def test_discovery_rejects_invalid_receipt_before_qualifying_refresh(application_feature_factory, invalid_receipt) -> None:
    feature = application_feature_factory("600001", NOW)
    feature = replace(feature, quote=replace(feature.quote, received_time=invalid_receipt))
    service = CandidateFilteringService(_recommendation_policy(StrategySettingsFixture()), None)

    with pytest.raises(ValueError, match="receipt time must be timezone-aware|after its evaluation time"):
        service.plan((feature,), None, evaluated_at=NOW + timedelta(seconds=45), data_version="invalid-receipt")


@pytest.mark.parametrize("source_offset", (timedelta(), -timedelta(minutes=1), timedelta(days=1)))
def test_discovery_receipt_basis_preserves_only_the_legal_preview_close(application_feature_factory, source_offset) -> None:
    received_at = NOW + timedelta(days=2)
    context = PreviewContext("slow-discovery-preview", NOW.date(), received_at)
    feature = application_feature_factory("600001", received_at)
    feature = replace(
        feature,
        quote=replace(feature.quote, source_time=context.market_as_of + source_offset),
        preview_context=context,
    )
    service = CandidateFilteringService(_recommendation_policy(StrategySettingsFixture()), None)

    plans = service.plan(
        (feature,), None, evaluated_at=received_at + timedelta(seconds=45), data_version="preview-discovery",
    )

    assert plans.physical_union() == ((feature.quote.code,) if not source_offset else ())
    for plan in plans.plans.values():
        assert plan.evaluations[0].features.quote.source_time == context.market_as_of + source_offset
        assert plan.evaluations[0].features.observed_at == received_at
