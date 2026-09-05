from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.application.ports.tomorrow_model import TomorrowModelInput
from trader.infra import tomorrow_production_model
from trader.infra.tomorrow_production_model import load_packaged_tomorrow_production_model

ROOT = Path(__file__).resolve().parents[3]


def test_packaged_production_model_is_hash_bound_and_predicts_deterministically() -> None:
    predictor = load_packaged_tomorrow_production_model("v2")
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
    assert first.predicted_excess_return == pytest.approx(-3.2489670901064623e-07)
    assert first.model_disagreement == pytest.approx(0.00015194486578771707)


def test_packaged_v1_model_is_independent_hash_bound_linear_inference() -> None:
    predictor = load_packaged_tomorrow_production_model("v1")
    row = TomorrowModelInput(
        code="600000",
        alpha_features=(0.01, -0.02, 0.03),
    )

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


def test_v1_and_v2_ignore_the_added_industry_input() -> None:
    for profile, width in (("v1", 3), ("v2", 6)):
        predictor = load_packaged_tomorrow_production_model(profile)
        values = (0.01, -0.02, 0.03, 0.02, -0.01, 0.04)[:width]
        plain = predictor.predict((TomorrowModelInput("600000", values),))[0]
        classified = predictor.predict((TomorrowModelInput("600000", values, "银行"),))[0]

        assert classified == plain


def test_trained_v3_model_is_self_hash_bound_and_report_is_optional(tmp_path: Path) -> None:
    p2 = json.loads((ROOT / "src/trader/resources/models/tomorrow_p2_model.json").read_text(encoding="utf-8"))
    payload: dict[str, object] = {
        "schema_version": "tomorrow_v3_production_model_v1",
        "profile_id": "v3",
        "model_id": "tomorrow_v3_industry_ridge_lightgbm",
        "feature_ids": [
            "qfq_return_1d",
            "qfq_return_3d",
            "qfq_return_5d",
            "qfq_residual_momentum_20d_skip5",
            "qfq_residual_momentum_40d_skip5",
            "qfq_residual_momentum_60d_skip5",
        ],
        "manifest_hash": "a" * 64,
        "split_hash": "b" * 64,
        "report_hash": "c" * 64,
        "training_anchor": "15:00_close",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "training_rows": 20_000,
        "validation_rows": 1_000,
        "industries": {
            "银行": {
                "transformer_means": [0.0] * 6,
                "transformer_scales": [1.0] * 6,
                "ridge_intercept": 0.0,
                "ridge_coefficients": [0.1] * 6,
                "lightgbm_model": p2["lightgbm_model"],
                "lightgbm_best_iteration": p2["lightgbm_best_iteration"],
                "calibration_intercept": 0.0,
                "calibration_slope": 1.0,
                "training_rows": 20_000,
                "validation_rows": 1_000,
            }
        },
        "dependencies": {"lightgbm": "4.7.0", "numpy": "2.0.0"},
        "automatic_model_update": False,
    }
    report: dict[str, object] = {
        "schema_version": "tomorrow_v3_training_report_v1",
        "model_id": payload["model_id"],
        "manifest_hash": payload["manifest_hash"],
        "split_hash": payload["split_hash"],
        "training_anchor": payload["training_anchor"],
        "runtime_anchor": payload["runtime_anchor"],
        "point_in_time_parity": payload["point_in_time_parity"],
        "industry_count": 1,
        "training_rows": payload["training_rows"],
        "validation_rows": payload["validation_rows"],
        "validation_passed": True,
        "failure_reasons": [],
        "automatic_model_update": payload["automatic_model_update"],
    }
    report_hash = tomorrow_production_model._content_hash(report)
    report["content_hash"] = report_hash
    payload["report_hash"] = report_hash
    payload["content_hash"] = tomorrow_production_model._content_hash(payload)
    run_root = tmp_path / "tomorrow-v3" / "run-1"
    run_root.mkdir(parents=True)
    (run_root / "model.json").write_text(json.dumps(payload), encoding="utf-8")
    assert not (run_root / "report.json").exists()

    predictor = load_packaged_tomorrow_production_model("v3", training_root=tmp_path)
    row = TomorrowModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行")

    assert predictor.profile_id == "v3"
    assert predictor.industry_ids == ("银行",)
    assert predictor.predict((row,)) == predictor.predict((row,))


def test_v3_profile_without_a_training_model_fails_with_a_stable_reason(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="training model is unavailable"):
        load_packaged_tomorrow_production_model("v3", training_root=tmp_path)


def test_v3_profile_rejects_a_tampered_training_model(tmp_path: Path) -> None:
    run_root = tmp_path / "tomorrow-v3" / "run-1"
    run_root.mkdir(parents=True)
    (run_root / "model.json").write_text(json.dumps({"content_hash": "a" * 64, "profile_id": "v3"}), encoding="utf-8")

    with pytest.raises(RuntimeError, match="training model is invalid"):
        load_packaged_tomorrow_production_model("v3", training_root=tmp_path)
