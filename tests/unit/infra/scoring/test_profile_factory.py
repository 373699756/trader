from __future__ import annotations

from pathlib import Path

import pytest

from trader.recommendation.domain.publication.models import Strategy
from trader.infra.scoring.profile_factory import load_scoring_profile

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def test_tracked_v3_two_head_bundles_load_with_expected_identity() -> None:
    profile = load_scoring_profile("v3", training_root=PROJECT_ROOT / "data" / "train")
    expected_hashes = {
        Strategy.TOMORROW: "a4f71a5365db7ceda1d52bbe65ef787c79b247f40add86b6d2404c8eebb0ce36",
        Strategy.D25: "83d49d9312aad690c28d98f9703016c38370d81d624ce6ebd3157e9a5883661d",
    }

    assert tuple(profile.heads) == (Strategy.TOMORROW, Strategy.D25)
    for strategy, expected_hash in expected_hashes.items():
        predictor = profile.heads[strategy].predictor
        assert predictor.profile_id == "v3"
        assert predictor.model_hash == expected_hash


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_profile_factory_fails_closed_without_shared_training_models(tmp_path: Path, profile: str) -> None:
    with pytest.raises(RuntimeError, match="shared strategy-head training models are unavailable"):
        load_scoring_profile(profile, training_root=tmp_path)
