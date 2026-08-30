from __future__ import annotations

from trader.application.ports.tomorrow_model import TomorrowModelInput
from trader.infra.tomorrow_production_model import load_packaged_tomorrow_production_model


def test_packaged_production_model_is_hash_bound_and_predicts_deterministically() -> None:
    predictor = load_packaged_tomorrow_production_model("p2")
    row = TomorrowModelInput(
        code="600000",
        alpha_features=(0.01, 0.02, 0.03, 0.01, -0.02, 0.03),
    )

    first = predictor.predict((row,))[0]
    second = predictor.predict((row,))[0]

    assert predictor.model_id == "daily_reconstructible_ensemble_v1"
    assert predictor.model_hash == "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"
    assert first == second
    assert first.code == "600000"
    assert first.model_disagreement >= 0.0


def test_packaged_p1_model_is_independent_hash_bound_linear_inference() -> None:
    predictor = load_packaged_tomorrow_production_model("p1")
    row = TomorrowModelInput(
        code="600000",
        alpha_features=(0.01, -0.02, 0.03),
    )

    first = predictor.predict((row,))[0]
    second = predictor.predict((row,))[0]

    assert predictor.profile_id == "p1"
    assert predictor.model_id == "p1_manual_residual_momentum_v1"
    assert len(predictor.model_hash) == 64
    assert predictor.feature_ids == (
        "qfq_residual_momentum_20d_skip5",
        "qfq_residual_momentum_40d_skip5",
        "qfq_residual_momentum_60d_skip5",
    )
    assert first == second
    assert first.model_disagreement == 0.0
