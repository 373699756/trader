from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.training.profiles.v3.profile import _publish_profile
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadProductionD25Profile, ScoringModelsUnavailableError
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.training.infra.d25.v3.contracts import V3_D25_PROFILE


def test_v3_d25_utility_without_probability_is_explicitly_unavailable(tmp_path: Path) -> None:
    training_root = tmp_path / "train"
    _publish_profile(training_root, V3_D25_PROFILE)
    with pytest.raises(ScoringModelsUnavailableError) as error:
        LoadProductionD25Profile(training_root, "v3", PredictionTarget(Strategy.D25, 100))
    assert error.value.code == "d25_target_model_missing"


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_profile_factory_fails_closed_without_shared_training_models(tmp_path: Path, profile: str) -> None:
    with pytest.raises(RuntimeError, match="production_model_missing"):
        LoadProductionD25Profile(tmp_path, profile, PredictionTarget(Strategy.D25, 100))
