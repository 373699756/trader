from __future__ import annotations

import threading
import time
from collections.abc import Sequence
from concurrent.futures import CancelledError
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.application.review_helpers import review
from tests.unit.application.test_input_runtime import _policy
from tests.unit.application.test_tomorrow_projection import EVALUATED_AT, _native_input, _verified_feature
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.pipeline.candidate_pool.candidate_builder import (
    BuildCandidatePlans,
    CandidatePlanningContext,
)
from trader.recommendation.application.pipeline.dynamic_filter.filter_executor import (
    FilterFeatureCandidates,
    ScoredSelectionIdentity,
    ScoredSelectionOptions,
)
from trader.recommendation.application.pipeline.final_selection.freeze_projection import (
    BuildScoredHybrid,
    BuildScoredLocal,
    ScoredProjectionInputs,
)
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.v1.scoring import LearnedRankScoringService
from trader.recommendation.application.ports.loaded_profile import (
    HeadRuntime,
    LoadedScoringProfile,
    ModelInput,
    ModelPrediction,
    ModelScoringContext,
    ModelScoringDeadlineError,
    ProfileEvidence,
)
from trader.recommendation.application.ports.scoring import D25NativeInput, TomorrowNativeInput
from trader.recommendation.domain.market.models import Board
from trader.recommendation.domain.candidate.filters import QuoteFreshnessBasis
from trader.recommendation.domain.evidence.review import RiskFact
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.freeze_identity import IsShadowFreeze, VisibleScoredSeats
from trader.recommendation.domain.publication.models import RecommendationAction, ScoredDisposition, Strategy
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.training.infra.profile.composition import SingleHeadCombiner


class _Predictor:
    model_id = "learned_rank"
    model_hash = "a" * 64
    feature_ids = LEARNED_RANK_FEATURE_IDS

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        return tuple(ModelPrediction(item.code, 0.02, item.alpha_features[0]) for item in inputs)


def _service(
    strategy: Strategy = Strategy.TOMORROW, predictor=None, *, production_authority: bool = True
) -> LearnedRankScoringService:
    head = HeadRuntime(
        strategy,
        predictor or _Predictor(),
        SingleHeadCombiner(),
        ProfileEvidence(
            "historical_unavailable", (), "manual_user_override", production_authority=production_authority
        ),
    )
    return LearnedRankScoringService(
        LoadedScoringProfile("v1", {strategy: head}),
        strategy,
        cost_policy=ExecutionCostPolicy(0.002, 1.0),
        content_hash=ContentHash,
    )


def _features(factory):
    result = []
    for index in range(12):
        board = (Board.MAIN, Board.CHINEXT, Board.STAR)[index // 4]
        code = f"{('600', '300', '688')[index // 4]}{index:03d}"
        feature = _verified_feature(factory(code, EVALUATED_AT - timedelta(seconds=10)))
        result.append(
            replace(
                feature,
                quote=replace(feature.quote, board=board),
                history_days=251,
                values={
                    **feature.values,
                    **dict.fromkeys(LEARNED_RANK_FEATURE_IDS, float(index)),
                    "entry_quality": 95.0 - index,
                    "qfq_amihud_20d": float(index),
                },
            )
        )
    return tuple(result)


def _population_qualifications(population, policy, strategy, evaluated_at):
    return FilterFeatureCandidates(
        population,
        policy,
        ScoredSelectionOptions(
            evaluated_at,
            30.0,
            phase="candidate_discovery",
            strategy=strategy,
            population_evaluated_at=evaluated_at,
            population_max_age_seconds=30.0,
            population_quote_freshness_basis=QuoteFreshnessBasis.RECEIPT,
        ),
        ScoredSelectionIdentity(evaluated_at.date(), "market:fixture", "market:fixture"),
    )


@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
@pytest.mark.parametrize("outcome", ("success", "failure", "cancelled", "deadline"))
def test_status_reads_complete_head_snapshot_while_prediction_is_blocked(
    application_feature_factory, strategy, outcome, monkeypatch, record_property,
) -> None:
    entered, release, status_ready = threading.Event(), threading.Event(), threading.Event()
    elapsed = [0.0]

    class BlockingPredictor(_Predictor):
        armed = False

        def predict(self, inputs):
            if self.armed:
                entered.set()
                assert release.wait(10), "test predictor was not released"
                if outcome == "failure":
                    raise RuntimeError("prediction_failed")
                if outcome == "cancelled":
                    raise CancelledError("prediction_cancelled")
                if outcome == "deadline":
                    elapsed[0] = 20.0
            return super().predict(inputs)

    predictor = BlockingPredictor()
    service = _service(strategy, predictor)
    monkeypatch.setattr(service, "_monotonic", lambda: elapsed[0])
    other_strategy = Strategy.D25 if strategy is Strategy.TOMORROW else Strategy.TOMORROW
    other_predictor = _Predictor()
    other_predictor.model_hash = "b" * 64
    router = ModelScoringRouter("v1", {strategy: service, other_strategy: _service(other_strategy, other_predictor)})
    population = _features(application_feature_factory)
    context = ModelScoringContext(population_features=population, population_identity="fixed", time_budget_seconds=10.0)
    original = service.score(population, context=context)
    before = router.status()
    predictor.armed = True
    results, errors, observations = [], [], []

    def score():
        try:
            results.append(service.score(population[:2], context=context))
        except (CancelledError, RuntimeError) as exc:
            errors.append(exc)

    def read_status():
        observations.append(router.status())
        status_ready.set()

    scoring = threading.Thread(target=score)
    reader = threading.Thread(target=read_status)
    scoring.start()
    try:
        assert entered.wait(5), "test predictor was not reached"
        started = time.perf_counter()
        reader.start()
        responsive = status_ready.wait(1)
        record_property("status_probe_wait_seconds", time.perf_counter() - started)
        record_property("status_ready_during_prediction", responsive)
        assert responsive, "read-only V1 status waited behind the blocked predictor"
        assert observations == [before]
    finally:
        release.set()
        scoring.join(5)
        if reader.ident is not None:
            reader.join(5)
        assert not scoring.is_alive() and not reader.is_alive()
    assert before is not None
    after = router.status()
    assert after.heads[other_strategy] == before.heads[other_strategy]
    completed = after.heads[strategy].computation
    assert completed.request_count == completed.predictor_batch_count == 2
    assert completed.candidate_count == 2
    assert completed.stage_durations[0].execution_count == 2
    assert replace(after.heads[strategy], computation=before.heads[strategy].computation) == before.heads[strategy]
    if outcome == "success":
        assert not errors and len(results) == 1
        assert results[0].rank_population_version == original.rank_population_version
        assert results[0].cost_reference_version == original.cost_reference_version
        assert dict(results[0].diagnostics) == {
            item.quote.code: original.diagnostics[item.quote.code] for item in population[:2]
        }
    else:
        expected_error = {
            "failure": RuntimeError, "cancelled": CancelledError, "deadline": ModelScoringDeadlineError
        }[outcome]
        assert not results and len(errors) == 1 and isinstance(errors[0], expected_error)
    predictor.armed = False
    service.score(population[:1], context=context)
    completed = service.status().computation
    assert completed.request_count == completed.predictor_batch_count == 3
    assert completed.candidate_count == 1
    assert completed.stage_durations[0].execution_count == 3
    assert router.status().heads[other_strategy] == before.heads[other_strategy]


@pytest.mark.parametrize("expires_during", ("inputs", "publication"))
def test_prediction_and_publication_honor_remaining_deadline(application_feature_factory, monkeypatch, expires_during):
    service = _service()
    population = _features(application_feature_factory)
    clock = [0.0]
    monkeypatch.setattr(service, "_monotonic", lambda: clock[0])
    eligible = service.is_input_eligible

    def slow_eligible(feature):
        clock[0] += 0.1
        return eligible(feature)

    def slow_hash(value):
        clock[0] += 1.0
        return ContentHash(value)

    if expires_during == "inputs":
        monkeypatch.setattr(service, "is_input_eligible", slow_eligible)
    else:
        monkeypatch.setattr(service, "_content_hash", slow_hash)
    with pytest.raises(ModelScoringDeadlineError):
        service.score(population, context=ModelScoringContext(time_budget_seconds=0.5))
    assert service.status().computation.predictor_batch_count == (0 if expires_during == "inputs" else 1)


def test_fixed_population_percentiles_cost_once_and_subset_order_are_deterministic(application_feature_factory) -> None:
    population = _features(application_feature_factory)
    service = _service()
    context = ModelScoringContext(population_features=population, population_identity="fixed")
    full = service.score(population, context=context)
    subset = service.score(
        tuple(reversed(population[3:8])), context=replace(context, population_features=tuple(reversed(population)))
    )
    assert full.score_scale == "learned_rank_percentile_0_100" and full.production_authority is True
    assert service.status().production_authority is True
    assert full.rank_population_version == subset.rank_population_version
    assert dict(subset.diagnostics) == {item.quote.code: full.diagnostics[item.quote.code] for item in population[3:8]}
    assert full.diagnostics[population[0].quote.code].signal_score == 0.0
    assert full.diagnostics[population[-1].quote.code].signal_score == 100.0
    for value in full.diagnostics.values():
        assert value.predicted_net_excess_pct == pytest.approx(
            value.predicted_excess_return_pct - value.estimated_cost_pct
        )
    single = service.score(population[:1])
    assert next(iter(single.diagnostics.values())).signal_score == 50.0
    assert service.score(()).diagnostics == {}
    with pytest.raises(ModelScoringDeadlineError):
        service.score(population, context=replace(context, time_budget_seconds=0.0))
    missing = replace(
        population[0],
        values={key: value for key, value in population[0].values.items() if key != LEARNED_RANK_FEATURE_IDS[0]},
    )
    assert service.score((missing,)).missing_codes == (missing.quote.code,)


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_slow_market_collection_keeps_discovery_population_for_fresh_candidate_model_ranks(
    application_feature_factory, native_type, record_property,
) -> None:
    batches = []

    class RecordingPredictor(_Predictor):
        def predict(self, inputs):
            batches.append(tuple(item.code for item in inputs))
            return super().predict(inputs)

    completed_at = EVALUATED_AT - timedelta(seconds=10)
    population = tuple(
        replace(
            feature,
            observed_at=completed_at,
            quote=replace(
                feature.quote,
                source_time=completed_at - timedelta(seconds=45 if index < 6 else 0),
                received_time=completed_at - timedelta(seconds=45 if index < 6 else 0),
            ),
        )
        for index, feature in enumerate(_features(application_feature_factory))
    )
    candidates = tuple(
        replace(
            feature,
            observed_at=EVALUATED_AT,
            quote=replace(
                feature.quote,
                source_time=EVALUATED_AT - timedelta(seconds=1),
                received_time=EVALUATED_AT - timedelta(seconds=1),
            ),
        )
        for feature in population[:2]
    )
    policy = _policy()
    router = ModelScoringRouter("v1", {native_type.strategy: _service(native_type.strategy, RecordingPredictor())})
    discovery = BuildCandidatePlans(
        population,
        None,
        CandidatePlanningContext(completed_at, "market:slow-collection", policy, router),
    )
    qualified = discovery.strategy_codes(native_type.strategy)
    assert set(qualified) == {item.quote.code for item in population}
    assert all(item.quote.age_seconds(EVALUATED_AT) == 1 for item in candidates)
    record_property("collection_span_seconds", 45)
    record_property("discovery_population_count", len(qualified))
    record_property("fresh_candidate_count", len(candidates))
    native = replace(
        _native_input(candidates, native_type),
        market_features=population,
        requested_codes=qualified,
    )

    projection = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(
            model_scoring=router,
            population_qualifications=_population_qualifications(population, policy, native_type.strategy, completed_at),
        ),
    )

    assert batches == [tuple(sorted(item.quote.code for item in population))]
    assert projection.input_quality.candidate_scored_count == 2
    expected = router.score(
        native_type.strategy,
        candidates,
        context=ModelScoringContext(
            population_features=population,
            population_identity=f"{native.trade_date.isoformat()}:{native.phase}:{native.config_version}",
        ),
    )
    assert projection.rank_population_version == expected.rank_population_version
    assert projection.cost_reference_version == expected.cost_reference_version
    assert dict(projection.model_diagnostics) == dict(expected.diagnostics)
    assert "stale_quote" not in projection.selection.population_filter_reason_counts
    assert tuple(item.quote.source_time for item in native.market_features) == tuple(
        item.quote.source_time for item in population
    )


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize("scenario", (
    "complete", "directed", "directed_missing_risk", "directed_window_unavailable", "directed_investigation",
    "directed_negative_announcement", "directed_external_risk", "rejected", "semantic_copy",
))
def test_frozen_discovery_preserves_complete_fresh_projection(application_feature_factory, native_type, scenario):
    population = _features(application_feature_factory)
    if scenario == "rejected":
        population = (*population[:-1], replace(population[-1], quote=replace(population[-1].quote, is_st=True)))
    if scenario in {"directed_missing_risk", "directed_window_unavailable"}:
        field_name, value = (
            ("official_investigation_history", None)
            if scenario == "directed_missing_risk" else ("corporate_risk_window_unavailable", 1.0)
        )
        population = (
            *population[:-1], replace(population[-1], values={**population[-1].values, field_name: value}),
        )
    if scenario in {"directed_investigation", "directed_negative_announcement"}:
        field_name = (
            "official_investigation_history"
            if scenario == "directed_investigation" else "negative_announcement_level"
        )
        population = (
            *population[:-1], replace(population[-1], values={**population[-1].values, field_name: 1.0}),
        )
    if scenario == "directed_external_risk":
        population = (*population[:-1], replace(population[-1], external_risk_facts=(
            RiskFact(
                "fixture:investigation", "official_investigation_history", "high", 25.0,
                "fixture", EVALUATED_AT - timedelta(seconds=10), veto=True,
            ),
        )))
    candidates = population[:2] if scenario.startswith("directed") else population
    native = replace(_native_input(candidates, native_type), market_features=population)
    policy = _policy()
    router = ModelScoringRouter("v1", {native_type.strategy: _service(native_type.strategy)})
    qualifications = _population_qualifications(population, policy, native_type.strategy, EVALUATED_AT)
    if scenario == "directed_investigation":
        assert qualifications.population.evaluations[population[-1].quote.code].disposition is ScoredDisposition.REJECT
    if scenario in {"directed_negative_announcement", "directed_external_risk"}:
        assert qualifications.population.evaluations[population[-1].quote.code].disposition is not ScoredDisposition.REJECT
    if scenario == "semantic_copy":
        qualifications = replace(
            qualifications,
            request=replace(
                qualifications.request,
                features=tuple(replace(item, values=dict(item.values)) for item in population),
            ),
        )
    expected = BuildScoredLocal(
        native, policy, sequence=1, runtime=ScoredProjectionInputs(model_scoring=router, monotonic=lambda: 0.0),
    )
    actual = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(
            model_scoring=router, population_qualifications=qualifications, monotonic=lambda: 0.0,
        ),
    )
    assert actual == expected


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_frozen_discovery_does_not_relax_current_candidate_freshness(application_feature_factory, native_type):
    population = _features(application_feature_factory)
    candidates = tuple(
        replace(item, quote=replace(item.quote, source_time=EVALUATED_AT - timedelta(seconds=31)))
        for item in population[:2]
    )
    native = replace(_native_input(candidates, native_type), market_features=population)
    policy = _policy()
    projection = BuildScoredLocal(
        native,
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(
            model_scoring=ModelScoringRouter("v1", {native_type.strategy: _service(native_type.strategy)}),
            population_qualifications=_population_qualifications(population, policy, native_type.strategy, EVALUATED_AT),
        ),
    )
    assert projection.input_quality.candidate_scored_count == 0
    assert projection.selection.scored_candidates == ()
    assert projection.selection.hard_filter_reason_counts["stale_quote"] == 2


@pytest.mark.parametrize("mismatch", ("features", "strategy", "phase", "future", "date", "directed", "policy", "window"))
def test_discovery_population_mismatch_fails_before_model_prediction(application_feature_factory, mismatch):
    population = _features(application_feature_factory)
    policy = _policy()
    qualifications = _population_qualifications(population, policy, Strategy.TOMORROW, EVALUATED_AT)
    request = qualifications.request
    if mismatch == "features":
        request = replace(request, features=(replace(population[0], values={**population[0].values, "entry_quality": 1.0}), *population[1:]))
    elif mismatch == "strategy":
        request = _population_qualifications(population, policy, Strategy.D25, EVALUATED_AT).request
    elif mismatch == "phase":
        request = replace(request, phase="final_review", population_quote_freshness_basis=QuoteFreshnessBasis.EVALUATION)
    elif mismatch == "future":
        request = replace(request, evaluated_at=EVALUATED_AT + timedelta(seconds=1), population_evaluated_at=EVALUATED_AT + timedelta(seconds=1))
    elif mismatch == "date":
        request = replace(request, trade_date=(EVALUATED_AT.date() - timedelta(days=1)).isoformat())
    elif mismatch == "directed":
        request = replace(request, candidate_features=population[:2])
    elif mismatch == "policy":
        request = replace(request, policy=replace(request.policy, hard_filter=replace(policy.hard_filter, blacklist_codes=frozenset({"999999"}))))
    elif mismatch == "window":
        request = replace(request, population_max_age_seconds=29.0)
    service = _service()
    with pytest.raises(ValueError, match="discovery population qualifications do not match"):
        BuildScoredLocal(
            _native_input(population),
            policy,
            sequence=1,
            runtime=ScoredProjectionInputs(
                model_scoring=ModelScoringRouter("v1", {Strategy.TOMORROW: service}),
                population_qualifications=replace(qualifications, request=request),
            ),
        )
    assert service.status().computation.request_count == 0


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
@pytest.mark.parametrize("production_authority", (True, False), ids=("production", "legacy_shadow"))
def test_v1_model_actually_changes_fixed_six_and_keeps_local_action_score(
    application_feature_factory, native_type, production_authority
) -> None:
    features = _features(application_feature_factory)
    strategy = native_type.strategy
    policy = _policy()
    weights = {
        board: {key: float(key == "entry_quality") for key in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(policy, board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights})
    router = ModelScoringRouter("v1", {strategy: _service(strategy, production_authority=production_authority)})
    projection = BuildScoredLocal(
        _native_input(tuple(reversed(features)), native_type),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=router),
    )
    seats = VisibleScoredSeats(projection.local)
    assert tuple(item.code for item in seats) == tuple(item.quote.code for item in reversed(features[6:]))
    assert all(item.final_score != item.local_score for item in seats)
    assert tuple(item.local_score for item in seats) == (84.0, 85.0, 86.0, 87.0, 88.0, 89.0)
    if production_authority:
        # Four stocks per board do not meet the existing reliability floor.
        assert all(item.reason == "filter_observe_only" for item in seats)
        assert projection.review_candidates == ()
        assert dict(projection.local.input_versions)["execution_mode"] == "production"
        assert dict(projection.local.input_versions)["production_authority"] == "true"
    else:
        assert all(item.action is not RecommendationAction.EXECUTABLE for item in seats)
    assert IsShadowFreeze(projection.local) is not production_authority
    assert dict(projection.local.input_versions)["rank_population"].startswith("learned-rank-population:")
    # A high model percentile never bypasses the existing local evidence threshold.
    weak = replace(features[-1], values={**features[-1].values, "entry_quality": 0.0})
    weaker = BuildScoredLocal(
        _native_input((*features[:-1], weak), native_type),
        policy,
        sequence=2,
        runtime=ScoredProjectionInputs(model_scoring=router),
    )
    assert VisibleScoredSeats(weaker.local)[0].action is RecommendationAction.UNAVAILABLE
    assert VisibleScoredSeats(weaker.local)[0].final_score == 100.0


@pytest.mark.parametrize("native_type", (TomorrowNativeInput, D25NativeInput))
def test_production_rank_allows_qualified_actions_and_review_preserves_ranking(
    application_feature_factory, native_type
):
    strategy = native_type.strategy
    policy = _policy()
    weights = {
        board: {key: float(key == "entry_quality") for key in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(policy, board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights})
    base = _features(application_feature_factory)[0]
    features = tuple(
        replace(
            base,
            quote=replace(base.quote, code=f"600{index:03d}"),
            values={
                **base.values,
                "completed_return_1d": float(index),
                "entry_quality": 90.0,
                "volume_to_5d_average": 0.6,
                "max_drawdown_20d": -2.0,
            },
        )
        for index in range(100)
    )
    projection = BuildScoredLocal(
        _native_input(features, native_type),
        policy,
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=ModelScoringRouter("v1", {strategy: _service(strategy)})),
    )
    seats = VisibleScoredSeats(projection.local)
    assert seats and all(item.action is RecommendationAction.EXECUTABLE for item in seats), [
        item.reason for item in seats
    ]
    code = projection.review_candidates[0].code
    applied = replace(review(code, 0.0), completed_at=EVALUATED_AT + timedelta(seconds=5))
    hybrid = BuildScoredHybrid(
        projection,
        policy,
        {code: applied},
        review_deadline=EVALUATED_AT.replace(hour=14, minute=48),
    )
    assert hybrid is not None and hybrid.freeze is not None
    assert hybrid.freeze.input_versions == projection.local.input_versions
    assert [(item.code, item.local_score, item.final_score) for item in VisibleScoredSeats(hybrid.freeze)] == [
        (item.code, item.local_score, item.final_score) for item in seats
    ]
    assert next(item for item in hybrid.freeze.items if item.code == code).review_outcome == "applied"


def test_non_positive_gross_minus_cost_closes_selection_before_local_score(application_feature_factory) -> None:
    class NonPositive(_Predictor):
        def predict(self, inputs):
            return tuple(ModelPrediction(item.code, 0.0, item.alpha_features[0]) for item in inputs)

    features = _features(application_feature_factory)
    router = ModelScoringRouter("v1", {Strategy.TOMORROW: _service(predictor=NonPositive())})
    projection = BuildScoredLocal(
        _native_input(features), _policy(), sequence=1, runtime=ScoredProjectionInputs(model_scoring=router)
    )
    assert VisibleScoredSeats(projection.local) == ()
    assert projection.stages.local_score.snapshot.input_count == 0


def test_missing_history_is_pending_without_invoking_v1_predictor(application_feature_factory) -> None:
    from trader.recommendation.domain.market.history_alignment import ModelHistoryQuality

    features = tuple(
        replace(feature, model_history_quality=ModelHistoryQuality("history_model_date_mismatch"))
        for feature in _features(application_feature_factory)
    )
    service = _service()
    projection = BuildScoredLocal(
        _native_input(features),
        _policy(),
        sequence=1,
        runtime=ScoredProjectionInputs(model_scoring=ModelScoringRouter("v1", {Strategy.TOMORROW: service})),
    )
    stage = projection.stages.model_filter.snapshot
    assert (stage.input_count, stage.output_count, stage.pending_count, stage.rejected_count, stage.failed_count) == (
        12,
        0,
        12,
        0,
        0,
    )
    assert [(reason.code, reason.count) for reason in stage.reasons] == [("history_model_date_mismatch", 12)]
    assert service.status().computation.request_count == 1
    assert service.status().computation.predictor_batch_count == 0
    service.score(_features(application_feature_factory))
    status = service.status().computation
    assert status.predictor_batch_count == 1 and status.stage_durations[0].last_duration_ms >= 0
