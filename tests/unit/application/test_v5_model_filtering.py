"""V5 consumes sequences while retaining the production prediction/cost contract."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.unit.application.scoring_helpers import profile_for
from tests.unit.application.test_tomorrow_production_model import NOW, _model_feature, _Predictor
from trader.recommendation.application.pipeline.local_score.model_scoring import ProductionModelScoringService
from trader.recommendation.application.ports.loaded_profile import (
    ModelPrediction,
    ModelScoringContext,
    ModelScoringDeadlineError,
)
from trader.recommendation.domain.market.v5.features import ModelPriceVolumeSequence
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError


class _SequencePredictor(_Predictor):
    profile_id = "v5"
    predicted = 0.02
    calls = 0

    def predict(self, inputs):
        self.calls += 1
        assert all(item.sequence is not None for item in inputs)
        return tuple(ModelPrediction(item.code, self.predicted) for item in inputs)


@pytest.mark.parametrize("prediction", (0.02, 0.0, -0.01))
def test_v5_sequence_prediction_cost_once_and_missing_input_closes_admission(application_feature_factory, prediction):
    predictor = _SequencePredictor()
    predictor.predicted = prediction
    service = ProductionModelScoringService(
        profile_for(predictor), Strategy.TOMORROW, cost_policy=ExecutionCostPolicy(0.002, 1.0)
    )
    base = _model_feature(application_feature_factory("600000", NOW), offset=0, amihud=1)
    dates = tuple(NOW.date() - timedelta(days=61 - i) for i in range(61))
    sequence = ModelPriceVolumeSequence("600000", dates, (0.01,) * 360, "publication")
    feature = replace(base, model_sequence=sequence)
    assert service.is_input_eligible(feature)
    batch = service.score((feature,))
    diagnostic = batch.diagnostics[feature.quote.code]
    assert diagnostic.predicted_net_excess_pct == pytest.approx(prediction * 100 - diagnostic.estimated_cost_pct)
    assert batch.score_scale == "weighted_evidence_quality_0_100"
    assert not service.is_input_eligible(base)
    calls = predictor.calls
    missing = service.score((base,))
    assert missing.missing_codes == ("600000",)
    assert not missing.predictions
    assert predictor.calls == calls
    future = replace(sequence, dates=tuple(day + timedelta(days=1) for day in dates))
    assert not service.is_input_eligible(replace(base, model_sequence=future))


def test_v5_missing_optional_runtime_is_classified_without_loading_other_models(tmp_path, monkeypatch):
    monkeypatch.setattr("trader.recommendation.infra.scoring.profile_factory.find_spec", lambda _name: None)
    with pytest.raises(ScoringModelsUnavailableError) as error:
        LoadScoringProfile("v5", training_root=tmp_path)
    assert error.value.reason == "dependency_missing"


def test_v5_inference_batches_are_bounded_and_report_actual_calls(application_feature_factory):
    predictor = _SequencePredictor()
    service = ProductionModelScoringService(
        profile_for(predictor), Strategy.TOMORROW, cost_policy=ExecutionCostPolicy(0.002, 1.0)
    )
    dates = tuple(NOW.date() - timedelta(days=61 - i) for i in range(61))
    features = tuple(
        replace(
            _model_feature(application_feature_factory(code := f"{600000 + i:06d}", NOW), offset=i * 0.0001, amihud=1),
            model_sequence=ModelPriceVolumeSequence(code, dates, (0.01,) * 360, "publication"),
        )
        for i in range(257)
    )
    result = service.score(features)
    assert len(result.predictions) == 257
    assert predictor.calls == service.computation_status().predictor_batch_count == 2
    assert service.score(features) == result
    assert predictor.calls == 2


def test_v5_late_batch_does_not_publish_a_result(application_feature_factory):
    clock = [0.0]

    class _LatePredictor(_SequencePredictor):
        def predict(self, inputs):
            output = super().predict(inputs)
            clock[0] = 2.0
            return output

    predictor = _LatePredictor()
    service = ProductionModelScoringService(
        profile_for(predictor),
        Strategy.TOMORROW,
        cost_policy=ExecutionCostPolicy(0.002, 1.0),
        monotonic=lambda: clock[0],
    )
    dates = tuple(NOW.date() - timedelta(days=61 - i) for i in range(61))
    feature = replace(
        _model_feature(application_feature_factory("600000", NOW), offset=0, amihud=1),
        model_sequence=ModelPriceVolumeSequence("600000", dates, (0.01,) * 360, "publication"),
    )
    with pytest.raises(ModelScoringDeadlineError, match="completed_after_batch_prediction_deadline"):
        service.score((feature,), context=ModelScoringContext(time_budget_seconds=1))
    assert predictor.calls == 1
    assert service.computation_status().deadline_abandon_reason == "completed_after_batch_prediction_deadline"
