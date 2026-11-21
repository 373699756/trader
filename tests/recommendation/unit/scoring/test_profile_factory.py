from __future__ import annotations

from pathlib import Path

import pytest

from tests.helpers.training.profiles.v3.profile import _publish_profile
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadD25UtilityProfile
from trader.training.infra.d25.v3.contracts import V3_D25_PROFILE


def test_v3_d25_ridge_bundle_loads_with_expected_identity(tmp_path: Path) -> None:
    training_root = tmp_path / "train"
    _publish_profile(training_root, V3_D25_PROFILE)
    profile = LoadD25UtilityProfile("v3", training_root=training_root)

    assert tuple(profile.heads) == (Strategy.D25,)
    predictor = profile.heads[Strategy.D25].predictor
    assert predictor.profile_id == "v3"
    assert predictor.model_id == V3_D25_PROFILE.head_for_strategy(Strategy.D25).model_id
    assert len(predictor.model_hash) == 64


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_profile_factory_fails_closed_without_shared_training_models(tmp_path: Path, profile: str) -> None:
    with pytest.raises(RuntimeError, match="production_model_missing"):
        LoadD25UtilityProfile(profile, training_root=tmp_path)
