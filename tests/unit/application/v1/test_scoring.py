from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.application.test_input_runtime import _policy
from tests.unit.application.test_tomorrow_projection import EVALUATED_AT, _native_input, _verified_feature
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
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
from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.decision_identity import IsShadowDecision, VisibleScoredSeats
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.training.infra.profile.composition import SingleHeadCombiner


class _Predictor:
    model_id = "learned_rank"
    model_hash = "a" * 64
    feature_ids = LEARNED_RANK_FEATURE_IDS

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        return tuple(ModelPrediction(item.code, 0.02, item.alpha_features[0]) for item in inputs)


def _service(strategy: Strategy = Strategy.TOMORROW, predictor=None) -> LearnedRankScoringService:
    head = HeadRuntime(
        strategy,
        predictor or _Predictor(),
        SingleHeadCombiner(),
        ProfileEvidence("historical_unavailable", (), "trained_artifact", production_authority=False),
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


def test_fixed_population_percentiles_cost_once_and_subset_order_are_deterministic(application_feature_factory) -> None:
    population = _features(application_feature_factory)
    service = _service()
    context = ModelScoringContext(population_features=population, population_identity="fixed")
    full = service.score(population, context=context)
    subset = service.score(
        tuple(reversed(population[3:8])), context=replace(context, population_features=tuple(reversed(population)))
    )
    assert full.score_scale == "learned_rank_percentile_0_100" and full.production_authority is False
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
def test_v1_model_actually_changes_fixed_six_and_keeps_local_action_score(
    application_feature_factory, native_type
) -> None:
    features = _features(application_feature_factory)
    strategy = native_type.strategy
    policy = _policy()
    weights = {
        board: {key: float(key == "entry_quality") for key in policy.board_local_strategy_weights[strategy][board]}
        for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    policy = replace(policy, board_local_strategy_weights={**policy.board_local_strategy_weights, strategy: weights})
    router = ModelScoringRouter("v1", {strategy: _service(strategy)})
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
    assert all(item.action is not RecommendationAction.EXECUTABLE for item in seats)
    assert IsShadowDecision(projection.local)
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


class _CountingPredictor(_Predictor):
    def __init__(self):
        self.calls = 0

    def predict(self, inputs):
        self.calls += 1
        return super().predict(inputs)


def test_prediction_reuse_keeps_current_cost_population_identity_and_subset(application_feature_factory):
    population = _features(application_feature_factory)
    predictor = _CountingPredictor()
    service = _service(predictor=predictor)
    context = ModelScoringContext(population_features=population, population_identity="first")
    original = service.score(population, context=context)
    changed = (
        replace(
            population[0],
            quote=replace(population[0].quote, price=99.0, received_time=EVALUATED_AT),
            values={**population[0].values, "qfq_amihud_20d": 100.0},
        ),
        *population[1:],
    )
    context = replace(context, population_features=tuple(reversed(changed)), population_identity="second")
    selected = tuple(reversed(changed[:4]))
    got = service.score(selected, context=context)
    assert got == _service().score(selected, context=context)
    assert got.cost_reference_version != original.cost_reference_version
    assert got.rank_population_version != original.rank_population_version
    assert (
        got.diagnostics[changed[0].quote.code].estimated_cost_pct
        != original.diagnostics[changed[0].quote.code].estimated_cost_pct
    )
    assert predictor.calls == 1
    assert service.status().computation.cache_hit_count == 1
    assert service.status().computation.stage_durations[0].last_duration_ms == 0.0
    with pytest.raises(ModelScoringDeadlineError):
        service.score(selected, context=replace(context, time_budget_seconds=0.0))
    assert predictor.calls == 1


@pytest.mark.parametrize("change", ("value", "population", "history", "model", "feature_order"))
def test_prediction_cache_rebuilds_complete_changed_model_inputs(application_feature_factory, change):
    population = _features(application_feature_factory)
    predictor = _CountingPredictor()
    service = _service(predictor=predictor)
    service.score(population)
    if change == "value":
        first = population[0]
        population = (replace(first, values={**first.values, LEARNED_RANK_FEATURE_IDS[0]: 0.123}), *population[1:])
    elif change == "population":
        population = population[:-1]
    elif change == "history":
        population = (replace(population[0], history_days=250), *population[1:])
    elif change == "model":
        predictor.model_hash = "b" * 64
    else:
        predictor.feature_ids = tuple(reversed(predictor.feature_ids))
    got = service.score(population)
    assert predictor.calls == 2
    assert got.predictions == _Predictor().predict(
        tuple(
            ModelInput(item.quote.code, tuple(float(item.values[name]) for name in predictor.feature_ids))
            for item in sorted(population, key=lambda item: item.quote.code)
            if service.is_input_eligible(item)
        )
    )
    if change == "history":
        assert got.missing_codes == (population[0].quote.code,)


@pytest.mark.parametrize("failure", ("wrong_codes", "no_rank", "exception", "timeout"))
def test_failed_prediction_does_not_replace_latest_successful_batch(application_feature_factory, failure):
    clock = [0.0]

    class Predictor(_CountingPredictor):
        mode = "valid"

        def predict(self, inputs):
            result = super().predict(inputs)
            if self.mode == "wrong_codes":
                return tuple(reversed(result))
            if self.mode == "no_rank":
                return tuple(replace(item, rank_raw=None) for item in result)
            if self.mode == "exception":
                raise ValueError("prediction failed")
            if self.mode == "timeout":
                clock[0] = 2.0
            return result

    predictor = Predictor()
    service = _service(predictor=predictor)
    service._monotonic = lambda: clock[0]
    population = _features(application_feature_factory)
    accepted = service.score(population)
    first = population[0]
    changed = (replace(first, values={**first.values, LEARNED_RANK_FEATURE_IDS[0]: 0.123}), *population[1:])
    predictor.mode = failure
    with pytest.raises((ValueError, ModelScoringDeadlineError)):
        service.score(changed, context=ModelScoringContext(time_budget_seconds=1.0))
    assert predictor.calls == 2
    predictor.mode = "valid"
    clock[0] = 0.0
    assert service.score(population) == accepted
    assert predictor.calls == 2
    assert service.score(changed) == _service().score(changed)
    assert predictor.calls == 3


def test_same_population_concurrent_calls_share_only_one_successful_prediction(application_feature_factory):
    entered, release = threading.Event(), threading.Event()

    class Predictor(_CountingPredictor):
        def predict(self, inputs):
            entered.set()
            assert release.wait(3)
            return super().predict(inputs)

    predictor = Predictor()
    service = _service(predictor=predictor)
    features = _features(application_feature_factory)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(service.score, features)
        assert entered.wait(3)
        second = executor.submit(service.score, tuple(reversed(features)))
        release.set()
        assert first.result(3) == second.result(3)
    assert predictor.calls == 1
    assert service.status().computation.cache_hit_count == 1
