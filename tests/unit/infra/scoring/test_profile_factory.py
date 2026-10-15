from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.training.infra.profile.v3.test_profile import _publish
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


def test_v3_two_head_ridge_bundles_load_with_expected_identity(tmp_path: Path) -> None:
    training_root = tmp_path / "train"
    for contract in V3_TRAINING_PROFILE.heads:
        _publish(
            training_root,
            contract,
            f"staging-{contract.strategy.value}",
            profile=V3_TRAINING_PROFILE,
            profile_owned=True,
        )
    profile = LoadScoringProfile("v3", training_root=training_root)

    assert tuple(profile.heads) == (Strategy.TOMORROW, Strategy.D25)
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        predictor = profile.heads[strategy].predictor
        assert predictor.profile_id == "v3"
        assert predictor.model_id == V3_TRAINING_PROFILE.head_for_strategy(strategy).model_id
        assert len(predictor.model_hash) == 64


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_profile_factory_fails_closed_without_shared_training_models(tmp_path: Path, profile: str) -> None:
    with pytest.raises(RuntimeError, match="shared strategy-head training models are unavailable"):
        LoadScoringProfile(profile, training_root=tmp_path)
