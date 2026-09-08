from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profile_factory import load_scoring_profile
from trader.infra.scoring.profiles.v3.bundle_codec import decode_tomorrow_bundle, load_tomorrow_bundle
from trader.infra.scoring.profiles.v3.bundle_locator import locate_latest_bundle
from trader.infra.scoring.profiles.v3.profile import build_scoring_profile, build_tomorrow_predictor


def _document() -> dict[str, object]:
    p2 = json.loads(
        resources.files("trader.infra.scoring.profiles.v2").joinpath("model.json").read_text(encoding="utf-8")
    )
    payload: dict[str, object] = {
        "schema_version": "tomorrow_production_model",
        "profile_id": "v3",
        "model_id": "industry_ridge_lightgbm",
        "strategy_head": "tomorrow",
        "feature_ids": [
            "qfq_return_1d",
            "qfq_return_3d",
            "qfq_return_5d",
            "qfq_residual_momentum_20d_skip5",
            "qfq_residual_momentum_40d_skip5",
            "qfq_residual_momentum_60d_skip5",
        ],
        "feature_units": ["decimal_return"] * 6,
        "exposure_contract": {
            "market": True,
            "board": True,
            "industry": True,
            "log_average_amount_20d": True,
            "order": ["market", "board", "industry", "log_average_amount_20d"],
        },
        "training_input_scope": "complete_manifest",
        "training_input_hash": "a" * 64,
        "training_input_codes": 100,
        "training_universe_codes": 100,
        "split_hash": "b" * 64,
        "report_hash": "c" * 64,
        "training_anchor": "15:00_close",
        "runtime_anchor": "14:50",
        "point_in_time_parity": False,
        "training_rows": 20_000,
        "validation_rows": 1_000,
        "industry_count": 1,
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
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
        "production_authority": False,
    }
    payload["content_hash"] = artifact_content_hash(payload)
    return payload


def _write_bundle(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_v3_locator_uses_the_direct_training_model(tmp_path: Path) -> None:
    model = tmp_path / "tomorrow-v3/model.json"
    legacy = tmp_path / "tomorrow-v3/input-hash/model.json"
    _write_bundle(model, _document())
    _write_bundle(legacy, _document())

    assert locate_latest_bundle(tmp_path) == model


def test_v3_locator_fails_closed_when_no_model_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="model.json"):
        locate_latest_bundle(tmp_path)


def test_v3_codec_profile_and_predictor_preserve_the_complete_contract(tmp_path: Path) -> None:
    path = tmp_path / "tomorrow-v3/model.json"
    document = _document()
    _write_bundle(path, document)

    artifact = load_tomorrow_bundle(path)
    predictor = build_tomorrow_predictor(artifact)
    profile = build_scoring_profile(artifact)
    loaded_profile = load_scoring_profile("v3", training_root=tmp_path)
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行")

    assert artifact.content_hash == document["content_hash"]
    assert predictor.predict((row,)) == predictor.predict((row,))
    assert predictor.industry_ids == ("银行",)
    assert predictor.exposure_contract.requires_industry is True
    assert tuple(head.strategy for head in profile.heads) == (Strategy.TOMORROW,)
    assert loaded_profile.heads[0].predictor.predict((row,)) == predictor.predict((row,))
    assert profile.evidence.historical_status == "historical_validated"
    prediction = predictor.predict((row,))[0]
    assert profile.combiner.combine((prediction,)) == prediction
    with pytest.raises(ValueError, match="exactly one"):
        profile.combiner.combine(())


def test_partial_v3_profile_scores_but_never_claims_historical_validation() -> None:
    document = _document()
    document.pop("content_hash")
    document["training_input_scope"] = "partial_checkpoint"
    document["training_input_codes"] = 60
    document["training_universe_codes"] = 100
    document["content_hash"] = artifact_content_hash(document)

    profile = build_scoring_profile(decode_tomorrow_bundle(document))
    prediction = profile.heads[0].predictor.predict(
        (ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行"),)
    )

    assert prediction[0].code == "600000"
    assert profile.evidence.historical_status == "historical_unavailable"
    assert profile.evidence.historical_failure_reasons == ("partial_history_pipeline_trial",)
    assert profile.evidence.activation_basis == "manual_user_override"


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("feature_units", "feature contract"),
        ("exposure_contract", "exposure contract"),
        ("ensemble_weights", "ensemble weights"),
    ),
)
def test_v3_codec_rejects_old_or_incomplete_contracts(field: str, message: str) -> None:
    document = _document()
    document.pop("content_hash")
    document.pop(field)
    document["content_hash"] = artifact_content_hash(document)

    with pytest.raises((TypeError, ValueError), match=message):
        decode_tomorrow_bundle(document)


def test_v3_codec_rejects_tampering_and_invalid_industry_models() -> None:
    tampered = _document()
    tampered["training_rows"] = 1
    with pytest.raises(ValueError, match="content hash"):
        decode_tomorrow_bundle(tampered)

    invalid = _document()
    invalid.pop("content_hash")
    industries = invalid["industries"]
    assert isinstance(industries, dict)
    bank = industries["银行"]
    assert isinstance(bank, dict)
    bank["transformer_scales"] = [0.0] * 6
    invalid["content_hash"] = artifact_content_hash(invalid)
    with pytest.raises(ValueError, match="industry model"):
        decode_tomorrow_bundle(invalid)

    unknown = _document()
    unknown.pop("content_hash")
    unknown["undefined_contract"] = True
    unknown["content_hash"] = artifact_content_hash(unknown)
    with pytest.raises(ValueError, match="fields are invalid"):
        decode_tomorrow_bundle(unknown)


def test_v3_predictor_rejects_uncovered_industry() -> None:
    predictor = build_tomorrow_predictor(decode_tomorrow_bundle(_document()))

    with pytest.raises(ValueError, match="industry is not covered"):
        predictor.predict((ModelInput("600000", (0.0,) * 6, "软件"),))


def test_v3_profile_rejects_an_invalid_lightgbm_model() -> None:
    document = _document()
    document.pop("content_hash")
    industries = document["industries"]
    assert isinstance(industries, dict)
    bank = industries["银行"]
    assert isinstance(bank, dict)
    bank["lightgbm_model"] = "not-a-lightgbm-model"
    document["content_hash"] = artifact_content_hash(document)

    artifact = decode_tomorrow_bundle(document)
    with pytest.raises(ValueError, match="LightGBM model is invalid"):
        build_tomorrow_predictor(artifact)
