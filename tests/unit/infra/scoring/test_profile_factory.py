from __future__ import annotations

from pathlib import Path

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.profile_factory import load_scoring_profile

PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _predictor(profile: str):
    return load_scoring_profile(profile).heads[Strategy.TOMORROW].predictor


def test_packaged_v1_profile_uses_strategy_keyed_head_contract() -> None:
    loaded = load_scoring_profile("v1")

    assert loaded.profile_id == "v1"
    assert tuple(loaded.heads) == (Strategy.TOMORROW,)
    assert loaded.heads[Strategy.TOMORROW].strategy is Strategy.TOMORROW


def test_profile_factory_preserves_v1_identity_and_linear_inference() -> None:
    predictor = _predictor("v1")
    row = ModelInput("600000", (0.01, -0.02, 0.03))

    first = predictor.predict((row,))[0]
    second = predictor.predict((row,))[0]

    assert predictor.profile_id == "v1"
    assert predictor.model_id == "residual_momentum_linear"
    assert predictor.model_hash == "4291ea514c233a14ab6f9262e72ea541d1e9a794e73d02f10f8220509f6f502b"
    assert predictor.feature_ids == (
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    assert first == second
    assert first.predicted_excess_return == pytest.approx(-2.5136160956193677e-05)
    assert first.model_disagreement == 0.0


def test_tracked_v3_three_head_bundles_load_with_expected_identity() -> None:
    profile = load_scoring_profile("v3", training_root=PROJECT_ROOT / "data" / "train")
    expected_hashes = {
        Strategy.TODAY: "940d251d0e303c3e1d94e70d42f561ff2bb89d554760815e0e8bc3ce057dc423",
        Strategy.TOMORROW: "a4f71a5365db7ceda1d52bbe65ef787c79b247f40add86b6d2404c8eebb0ce36",
        Strategy.D25: "83d49d9312aad690c28d98f9703016c38370d81d624ce6ebd3157e9a5883661d",
    }

    assert tuple(profile.heads) == (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    for strategy, expected_hash in expected_hashes.items():
        predictor = profile.heads[strategy].predictor
        assert predictor.profile_id == "v3"
        assert predictor.model_hash == expected_hash


def test_profile_factory_preserves_v1_independence_from_industry_input() -> None:
    predictor = _predictor("v1")
    values = (0.01, -0.02, 0.03)
    plain = predictor.predict((ModelInput("600000", values),))[0]
    classified = predictor.predict((ModelInput("600000", values, "银行"),))[0]

    assert classified == plain


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_profile_factory_fails_closed_without_shared_training_models(tmp_path: Path, profile: str) -> None:
    with pytest.raises(RuntimeError, match="shared strategy-head training models are unavailable"):
        load_scoring_profile(profile, training_root=tmp_path)
