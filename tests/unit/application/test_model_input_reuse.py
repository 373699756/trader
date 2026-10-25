"""Input reuse remains bound to full-population facts and sequence identity."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.application.test_tomorrow_production_model import NOW, _model_feature, _Predictor, _service
from tests.unit.application.test_v5_model_filtering import _SequencePredictor
from trader.recommendation.application.pipeline.local_score import model_scoring
from trader.recommendation.application.ports.loaded_profile import ModelScoringContext, ModelScoringDeadlineError
from trader.recommendation.domain.market.feature_contracts import V2_TOMORROW_MODEL_FEATURE_MANIFEST
from trader.recommendation.domain.market.v5.features import ModelPriceVolumeSequence


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_model_vectors_reuse_same_facts_but_refresh_cost_and_full_population(
    application_feature_factory, monkeypatch, profile
):
    predictor = _Predictor()
    predictor.profile_id = profile
    if profile == "v2":
        predictor.feature_ids = V2_TOMORROW_MODEL_FEATURE_MANIFEST.names
    population = tuple(
        replace(
            item := _model_feature(application_feature_factory(f"60000{i}", NOW), offset=i / 100, amihud=i + 1),
            history_days=251,
            values={**item.values, "qfq_momentum_120d_skip5": 0.2 + i / 100, "qfq_momentum_250d_skip5": 0.3 + i / 100},
        )
        for i in range(3)
    )
    context = ModelScoringContext(population_features=population, population_identity="fixed")
    service = _service(predictor)
    accepted = service.score(population[:2], context=context)
    original = model_scoring._model_feature_vector

    def forbidden(*args):
        raise AssertionError("unchanged model input rebuilt a vector")

    monkeypatch.setattr(model_scoring, "_model_feature_vector", forbidden)
    assert service.score(tuple(reversed(population[:2])), context=context) == accepted
    changed = (replace(population[0], values={**population[0].values, "qfq_amihud_20d": 100.0}), *population[1:])
    context = replace(context, population_features=changed, population_identity="new-phase")
    got = service.score(changed[:2], context=context)
    assert got.predictions == accepted.predictions
    assert got.cost_reference_version != accepted.cost_reference_version
    assert got.diagnostics != accepted.diagnostics
    with pytest.raises(ModelScoringDeadlineError):
        service.score(changed[:2], context=replace(context, time_budget_seconds=0.0))
    assert service.computation_status().stage_durations
    assert (
        next(
            s.execution_count
            for s in service.computation_status().stage_durations
            if s.calculator_group == "model_input_vectors"
        )
        == 1
    )
    monkeypatch.setattr(model_scoring, "_model_feature_vector", original)
    assert got == _service(predictor).score(changed[:2], context=context)

    calls = []

    def record(index, vector_context):
        calls.append(index)
        return original(index, vector_context)

    monkeypatch.setattr(model_scoring, "_model_feature_vector", record)
    peer = changed[-1]
    field = "qfq_momentum_250d_skip5" if profile == "v2" else "qfq_momentum_20d_skip5"
    peer = replace(peer, values={**peer.values, field: 0.42})
    context = replace(context, population_features=(*changed[:-1], peer))
    revised = service.score(changed[:2], context=context)
    assert calls == [0, 1]
    assert revised == _service(predictor).score(changed[:2], context=context)
    calls.clear()
    subset = service.score(changed[:1], context=context)
    assert calls == [0]
    assert subset == _service(predictor).score(changed[:1], context=context)


@pytest.mark.parametrize("change", ("values", "dates", "publication"))
def test_v5_vector_reuse_rechecks_complete_sequence_identity(application_feature_factory, monkeypatch, change):
    predictor = _SequencePredictor()
    service = _service(predictor)
    dates = tuple(NOW.date() - timedelta(days=61 - i) for i in range(61))
    sequence = ModelPriceVolumeSequence("600000", dates, (0.01,) * 360, "first")
    feature = replace(
        _model_feature(application_feature_factory("600000", NOW), offset=0, amihud=1), model_sequence=sequence
    )
    first = service.score((feature,))
    original = model_scoring._model_feature_vector

    def forbidden(*args):
        raise AssertionError("same sequence rebuilt a vector")

    monkeypatch.setattr(model_scoring, "_model_feature_vector", forbidden)
    assert service.score((feature,)) == first
    assert predictor.calls == 1
    monkeypatch.setattr(model_scoring, "_model_feature_vector", original)
    if change == "values":
        sequence = replace(sequence, values=(0.010000000000000002, *sequence.values[1:]))
    elif change == "dates":
        sequence = replace(sequence, dates=tuple(day - timedelta(days=1) for day in dates))
    else:
        sequence = replace(sequence, source_identity="corrected")
    revised = replace(feature, model_sequence=sequence)
    got = service.score((revised,))
    assert predictor.calls == 2
    assert service._state.inputs[0].sequence == sequence
    assert got == _service(_SequencePredictor()).score((revised,))
