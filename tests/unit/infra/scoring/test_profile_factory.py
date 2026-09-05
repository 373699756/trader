from __future__ import annotations

from pathlib import Path

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.infra.scoring.profile_factory import load_scoring_profile


def _predictor(profile: str):
    return load_scoring_profile(profile).heads[0].predictor


def test_profile_factory_preserves_v2_identity_and_deterministic_prediction() -> None:
    predictor = _predictor("v2")
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03))

    first = predictor.predict((row,))[0]
    second = predictor.predict((row,))[0]

    assert predictor.model_id == "daily_reconstructible_ensemble_v1"
    assert predictor.model_hash == "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"
    assert first == second
    assert first.code == "600000"
    assert first.predicted_excess_return == pytest.approx(-3.2489670901064623e-07)
    assert first.model_disagreement == pytest.approx(0.00015194486578771707)


def test_profile_factory_preserves_v1_identity_and_linear_inference() -> None:
    predictor = _predictor("v1")
    row = ModelInput("600000", (0.01, -0.02, 0.03))

    first = predictor.predict((row,))[0]
    second = predictor.predict((row,))[0]

    assert predictor.profile_id == "v1"
    assert predictor.model_id == "v1_manual_residual_momentum_v1"
    assert predictor.model_hash == "4291ea514c233a14ab6f9262e72ea541d1e9a794e73d02f10f8220509f6f502b"
    assert predictor.feature_ids == (
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    assert first == second
    assert first.predicted_excess_return == pytest.approx(-2.5136160956193677e-05)
    assert first.model_disagreement == 0.0


def test_profile_factory_keeps_v1_and_v2_independent_of_industry_input() -> None:
    for profile, width in (("v1", 3), ("v2", 6)):
        predictor = _predictor(profile)
        values = (0.01, -0.02, 0.03, 0.02, -0.01, 0.04)[:width]
        plain = predictor.predict((ModelInput("600000", values),))[0]
        classified = predictor.predict((ModelInput("600000", values, "银行"),))[0]

        assert classified == plain


def test_profile_factory_fails_closed_without_a_v3_training_model(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="training model is unavailable"):
        load_scoring_profile("v3", training_root=tmp_path)
