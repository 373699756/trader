from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.training.infra.profile.v3.test_profile import _publish_profile
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


def test_v3_two_head_ridge_bundles_load_with_expected_identity(tmp_path: Path) -> None:
    training_root = tmp_path / "train"
    _publish_profile(training_root, V3_TRAINING_PROFILE)
    profile = LoadScoringProfile("v3", training_root=training_root)

    assert tuple(profile.heads) == (Strategy.TOMORROW, Strategy.D25)
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        predictor = profile.heads[strategy].predictor
        assert predictor.profile_id == "v3"
        assert predictor.model_id == V3_TRAINING_PROFILE.head_for_strategy(strategy).model_id
        assert len(predictor.model_hash) == 64


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_profile_factory_fails_closed_without_shared_training_models(tmp_path: Path, profile: str) -> None:
    with pytest.raises(RuntimeError, match="production_model_missing"):
        LoadScoringProfile(profile, training_root=tmp_path)
