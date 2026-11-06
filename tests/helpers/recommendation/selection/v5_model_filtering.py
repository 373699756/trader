"""V5 consumes sequences while retaining the production prediction/cost contract."""

from tests.helpers.recommendation.scoring.tomorrow_production_model import _Predictor
from trader.recommendation.application.ports.loaded_profile import ModelPrediction


class _SequencePredictor(_Predictor):
    profile_id = "v5"
    model_id = "v5_industry_tcn"
    predicted = 0.02
    calls = 0

    def predict(self, inputs):
        self.calls += 1
        assert all(item.sequence is not None for item in inputs)
        return tuple(ModelPrediction(item.code, self.predicted) for item in inputs)
