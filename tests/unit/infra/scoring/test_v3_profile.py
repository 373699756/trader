from __future__ import annotations

import json
import os
from datetime import date
from importlib import resources
from pathlib import Path

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profile_factory import load_scoring_profile
from trader.infra.scoring.profiles.v3.bundle_codec import decode_tomorrow_bundle, load_tomorrow_bundle
from trader.infra.scoring.profiles.v3.bundle_locator import locate_latest_bundle
from trader.infra.scoring.profiles.v3.profile import build_scoring_profile, build_tomorrow_predictor
from trader.infra.scoring.profiles.v3.training_bundle_repository import (
    inspect_active_tomorrow_bundle,
    publish_tomorrow_bundle,
    recover_tomorrow_bundle_publication,
)


def _document() -> dict[str, object]:
    p2 = json.loads(
        resources.files("trader.infra.scoring.profiles.v2").joinpath("model.json").read_text(encoding="utf-8")
    )
    payload: dict[str, object] = {
        "schema_version": "tomorrow_scoring_model",
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
        "label_cutoff": "2026-09-08",
        "source_identity_hash": "1" * 64,
        "training_input_document_hash": "3" * 64,
        "training_input_codes": 100,
        "training_universe_codes": 100,
        "split_hash": "b" * 64,
        "report_hash": "c" * 64,
        "source_commit": "d" * 40,
        "feature_manifest_hash": TOMORROW_MODEL_FEATURE_MANIFEST.content_hash,
        "training_contract_hash": "e" * 64,
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "training_anchor": "15:00_close_proxy",
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
    payload["model_payload_hash"] = _model_payload_hash(payload)
    payload["content_hash"] = artifact_content_hash(payload)
    return payload


def _model_payload_hash(document: dict[str, object]) -> str:
    return artifact_content_hash(
        {
            key: value
            for key, value in document.items()
            if key not in {"content_hash", "report_hash", "model_payload_hash"}
        }
    )


def _report(document: dict[str, object]) -> dict[str, object]:
    report: dict[str, object] = {
        "schema_version": "tomorrow_training_report",
        "model_id": document["model_id"],
        "training_input_scope": document["training_input_scope"],
        "training_input_hash": document["training_input_hash"],
        "label_cutoff": document["label_cutoff"],
        "source_identity_hash": document["source_identity_hash"],
        "training_input_document_hash": document["training_input_document_hash"],
        "training_input_codes": document["training_input_codes"],
        "training_universe_codes": document["training_universe_codes"],
        "feature_manifest_hash": document["feature_manifest_hash"],
        "split_hash": document["split_hash"],
        "source_commit": document["source_commit"],
        "training_contract_hash": document["training_contract_hash"],
        "model_payload_hash": document["model_payload_hash"],
        "label_target": document["label_target"],
        "training_cost_bps": document["training_cost_bps"],
        "training_anchor": document["training_anchor"],
        "runtime_anchor": document["runtime_anchor"],
        "point_in_time_parity": document["point_in_time_parity"],
        "validation_scope": document["validation_scope"],
        "historical_status": document["historical_status"],
        "historical_failure_reasons": document["historical_failure_reasons"],
        "industry_count": document["industry_count"],
        "training_rows": document["training_rows"],
        "validation_rows": document["validation_rows"],
        "validation_passed": True,
        "failure_reasons": [],
        "automatic_model_update": False,
        "production_authority": False,
    }
    report["content_hash"] = artifact_content_hash(report)
    return report


def _write_bundle(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    training_input = _training_input(document)
    document["training_input_document_hash"] = training_input["content_hash"]
    document.pop("content_hash", None)
    document["model_payload_hash"] = _model_payload_hash(document)
    report = _report(document)
    document["report_hash"] = report["content_hash"]
    document["content_hash"] = artifact_content_hash(document)
    path.write_text(json.dumps(document), encoding="utf-8")
    path.with_name("report.json").write_text(json.dumps(report), encoding="utf-8")
    path.with_name("training-input.json").write_text(json.dumps(training_input), encoding="utf-8")


def _training_input(document: dict[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "tomorrow_training_input",
        "training_input_scope": "complete_manifest",
        "training_input_hash": document["training_input_hash"],
        "label_cutoff": document["label_cutoff"],
        "source_identity_hash": document["source_identity_hash"],
        "calendar_hash": "4" * 64,
        "source_cutoff": "2026-09-09",
        "requested_sessions": 2000,
        "input_descriptor_hash": "5" * 64,
        "training_input_codes": document["training_input_codes"],
        "training_universe_codes": document["training_universe_codes"],
        "codes": [f"{index:06d}" for index in range(100)],
        "source_commit": document["source_commit"],
        "feature_manifest_hash": document["feature_manifest_hash"],
        "training_contract_hash": document["training_contract_hash"],
        "label_target": "pre_cost_excess_return",
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "production_authority": False,
    }
    payload["content_hash"] = artifact_content_hash(payload)
    return payload


def test_v3_locator_uses_only_the_four_fixed_profile_files(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    _write_bundle(staging / "model.json", _document())
    selected = publish_tomorrow_bundle(
        staging,
        tmp_path / "tomorrow-v3",
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )

    assert locate_latest_bundle(tmp_path) == selected
    assert selected == tmp_path / "tomorrow-v3/model.json"
    assert sorted(path.name for path in selected.parent.iterdir()) == [
        "active-bundle.json",
        "model.json",
        "report.json",
        "training-input.json",
    ]

    repeated_staging = tmp_path / "repeated-staging"
    _write_bundle(repeated_staging / "model.json", _document())
    repeated = publish_tomorrow_bundle(
        repeated_staging,
        tmp_path / "tomorrow-v3",
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )
    assert repeated == selected
    assert not (tmp_path / "tomorrow-v3/generations").exists()


def test_v3_locator_fails_closed_when_no_model_exists(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="active-bundle.json"):
        locate_latest_bundle(tmp_path)


def test_v3_failed_staging_validation_preserves_the_previous_active_group(tmp_path: Path) -> None:
    first_staging = tmp_path / "first-staging"
    _write_bundle(first_staging / "model.json", _document())
    first = publish_tomorrow_bundle(
        first_staging,
        tmp_path / "tomorrow-v3",
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )
    pointer_before = (tmp_path / "tomorrow-v3/active-bundle.json").read_bytes()
    broken_staging = tmp_path / "broken-staging"
    _write_bundle(broken_staging / "model.json", _document())
    training_input = json.loads((broken_staging / "training-input.json").read_text(encoding="utf-8"))
    training_input["source_identity_hash"] = "f" * 64
    (broken_staging / "training-input.json").write_text(json.dumps(training_input), encoding="utf-8")

    with pytest.raises(ValueError, match="training input"):
        publish_tomorrow_bundle(
            broken_staging,
            tmp_path / "tomorrow-v3",
            training_input_hash="a" * 64,
            source_identity_hash="1" * 64,
            label_cutoff=date(2026, 9, 8),
        )

    assert locate_latest_bundle(tmp_path) == first
    assert (tmp_path / "tomorrow-v3/active-bundle.json").read_bytes() == pointer_before


def test_v3_failed_fixed_file_replacement_restores_the_previous_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "tomorrow-v3"
    first_staging = tmp_path / "first-staging"
    _write_bundle(first_staging / "model.json", _document())
    publish_tomorrow_bundle(
        first_staging,
        output,
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )
    previous = {
        name: (output / name).read_bytes()
        for name in (
            "training-input.json",
            "report.json",
            "model.json",
            "active-bundle.json",
        )
    }
    second_staging = tmp_path / "second-staging"
    changed = _document()
    changed["source_commit"] = "f" * 40
    _write_bundle(second_staging / "model.json", changed)
    original_replace = os.replace

    def fail_report(source: str | Path, destination: str | Path) -> None:
        if Path(source) == second_staging / "report.json":
            raise OSError("forced replacement failure")
        original_replace(source, destination)

    monkeypatch.setattr(
        "trader.infra.scoring.profiles.v3.training_bundle_repository.os.replace",
        fail_report,
    )

    with pytest.raises(OSError, match="forced replacement failure"):
        publish_tomorrow_bundle(
            second_staging,
            output,
            training_input_hash="a" * 64,
            source_identity_hash="1" * 64,
            label_cutoff=date(2026, 9, 8),
        )

    assert {name: (output / name).read_bytes() for name in previous} == previous
    assert not (output / ".bundle-publication.json").exists()
    assert not tuple(output.glob(".bundle-rollback.*"))


def test_v3_codec_profile_and_predictor_preserve_the_complete_contract(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    path = staging / "model.json"
    document = _document()
    _write_bundle(path, document)
    path = publish_tomorrow_bundle(
        staging,
        tmp_path / "tomorrow-v3",
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )

    artifact = load_tomorrow_bundle(path)
    predictor = build_tomorrow_predictor(artifact)
    profile = build_scoring_profile(artifact)
    loaded_profile = load_scoring_profile("v3", training_root=tmp_path)
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行")

    assert artifact.content_hash == document["content_hash"]
    assert artifact.label_cutoff.isoformat() == "2026-09-08"
    pointer = json.loads((tmp_path / "tomorrow-v3/active-bundle.json").read_text(encoding="utf-8"))
    assert set(pointer) == {
        "model_hash",
        "report_hash",
        "training_input_document_hash",
        "content_hash",
    }
    assert predictor.predict((row,)) == predictor.predict((row,))
    assert predictor.industry_ids == ("银行",)
    assert predictor.exposure_contract.requires_industry is True
    assert tuple(head.strategy for head in profile.heads) == (Strategy.TOMORROW,)
    assert loaded_profile.heads[0].predictor.predict((row,)) == predictor.predict((row,))
    assert profile.evidence.historical_status == "historical_data_insufficient"
    assert profile.evidence.historical_failure_reasons == ("daily_close_proxy_not_point_in_time",)
    assert profile.evidence.activation_basis == "manual_user_override"
    assert profile.evidence.training_anchor == "15:00_close_proxy"
    prediction = predictor.predict((row,))[0]
    assert profile.combiner.combine((prediction,)) == prediction
    with pytest.raises(ValueError, match="exactly one"):
        profile.combiner.combine(())


def test_partial_checkpoint_v3_profile_is_rejected_by_the_active_archive_contract() -> None:
    document = _document()
    document.pop("content_hash")
    document["training_input_scope"] = "partial_checkpoint"
    document["training_input_codes"] = 60
    document["training_universe_codes"] = 100
    document["historical_failure_reasons"] = [
        "daily_close_proxy_not_point_in_time",
        "partial_history_pipeline_trial",
    ]
    document["model_payload_hash"] = _model_payload_hash(document)
    document["content_hash"] = artifact_content_hash(document)

    with pytest.raises(ValueError, match="identity"):
        decode_tomorrow_bundle(document)


@pytest.mark.parametrize(
    ("field", "message"),
    (
        ("feature_units", "feature contract"),
        ("exposure_contract", "exposure contract"),
        ("ensemble_weights", "ensemble weights"),
        ("label_cutoff", "fields"),
    ),
)
def test_v3_codec_rejects_old_or_incomplete_contracts(field: str, message: str) -> None:
    document = _document()
    document.pop("content_hash")
    document.pop(field)
    document["content_hash"] = artifact_content_hash(document)

    with pytest.raises((TypeError, ValueError), match=message):
        decode_tomorrow_bundle(document)


def test_v3_codec_rejects_the_old_unpaired_model_schema() -> None:
    document = _document()
    document.pop("content_hash")
    document["schema_version"] = "tomorrow_production_model"
    document["content_hash"] = artifact_content_hash(document)

    with pytest.raises(ValueError, match="identity or feature contract"):
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
    invalid["model_payload_hash"] = _model_payload_hash(invalid)
    invalid["content_hash"] = artifact_content_hash(invalid)
    with pytest.raises(ValueError, match="industry model"):
        decode_tomorrow_bundle(invalid)

    unknown = _document()
    unknown.pop("content_hash")
    unknown["undefined_contract"] = True
    unknown["content_hash"] = artifact_content_hash(unknown)
    with pytest.raises(ValueError, match="fields are invalid"):
        decode_tomorrow_bundle(unknown)


def test_v3_loader_rejects_a_missing_or_mismatched_report_pair(tmp_path: Path) -> None:
    model = tmp_path / "tomorrow-v3/model.json"
    model.parent.mkdir(parents=True)
    model.write_text(json.dumps(_document()), encoding="utf-8")

    with pytest.raises(FileNotFoundError, match="report.json"):
        load_tomorrow_bundle(model)

    _write_bundle(model, _document())
    report = json.loads(model.with_name("report.json").read_text(encoding="utf-8"))
    report["training_contract_hash"] = "f" * 64
    report["content_hash"] = artifact_content_hash(
        {key: value for key, value in report.items() if key != "content_hash"}
    )
    model.with_name("report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="group"):
        load_tomorrow_bundle(model)


def test_v3_loader_rejects_a_missing_or_mismatched_training_input(tmp_path: Path) -> None:
    model = tmp_path / "tomorrow-v3/model.json"
    _write_bundle(model, _document())
    model.with_name("training-input.json").unlink()
    with pytest.raises(FileNotFoundError, match="training-input.json"):
        load_tomorrow_bundle(model)

    _write_bundle(model, _document())
    training_input = json.loads(model.with_name("training-input.json").read_text(encoding="utf-8"))
    training_input["source_identity_hash"] = "f" * 64
    training_input["content_hash"] = artifact_content_hash(
        {key: value for key, value in training_input.items() if key != "content_hash"}
    )
    model.with_name("training-input.json").write_text(json.dumps(training_input), encoding="utf-8")
    with pytest.raises(ValueError, match="group"):
        load_tomorrow_bundle(model)


def test_v3_locator_rejects_a_pointer_with_a_different_model_hash(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    _write_bundle(staging / "model.json", _document())
    publish_tomorrow_bundle(
        staging,
        tmp_path / "tomorrow-v3",
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )
    pointer_path = tmp_path / "tomorrow-v3/active-bundle.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    pointer["model_hash"] = "f" * 64
    pointer["content_hash"] = artifact_content_hash(
        {key: value for key, value in pointer.items() if key != "content_hash"}
    )
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        locate_latest_bundle(tmp_path)


def test_v3_locator_fails_closed_while_a_flat_publication_is_incomplete(tmp_path: Path) -> None:
    output = tmp_path / "tomorrow-v3"
    staging = tmp_path / "staging"
    _write_bundle(staging / "model.json", _document())
    publish_tomorrow_bundle(
        staging,
        output,
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )
    (output / ".bundle-publication.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RuntimeError, match="publication is incomplete"):
        inspect_active_tomorrow_bundle(output)


def test_flat_publication_recovery_restores_the_previous_four_files(tmp_path: Path) -> None:
    output = tmp_path / "tomorrow-v3"
    first_staging = tmp_path / "first-staging"
    _write_bundle(first_staging / "model.json", _document())
    publish_tomorrow_bundle(
        first_staging,
        output,
        training_input_hash="a" * 64,
        source_identity_hash="1" * 64,
        label_cutoff=date(2026, 9, 8),
    )
    previous = {
        name: (output / name).read_bytes()
        for name in (
            "training-input.json",
            "report.json",
            "model.json",
            "active-bundle.json",
        )
    }
    rollback = output / ".bundle-rollback.test"
    rollback.mkdir()
    for name, content in previous.items():
        (rollback / name).write_bytes(content)
    (output / "model.json").write_text("{}", encoding="utf-8")
    journal = {
        "schema_version": "tomorrow_training_bundle_publication",
        "rollback_directory": rollback.name,
        "previous_files": list(previous),
        "new_pointer_hash": "f" * 64,
    }
    journal["content_hash"] = artifact_content_hash(journal)
    (output / ".bundle-publication.json").write_text(json.dumps(journal), encoding="utf-8")

    recover_tomorrow_bundle_publication(output)

    assert {name: (output / name).read_bytes() for name in previous} == previous
    assert not rollback.exists()
    assert not (output / ".bundle-publication.json").exists()


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
    document["model_payload_hash"] = _model_payload_hash(document)
    document["content_hash"] = artifact_content_hash(document)

    artifact = decode_tomorrow_bundle(document)
    with pytest.raises(ValueError, match="LightGBM model is invalid"):
        build_tomorrow_predictor(artifact)
