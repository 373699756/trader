from __future__ import annotations

from collections.abc import Sequence
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
