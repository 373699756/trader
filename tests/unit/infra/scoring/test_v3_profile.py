from __future__ import annotations

import json
import os
from datetime import date
from importlib import resources
from pathlib import Path

import pytest

from trader.application.ports.model_scoring import ModelInput
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.artifact_hashing import artifact_content_hash
from trader.infra.scoring.profile_factory import load_scoring_profile
from trader.infra.scoring.profiles.v3.bundle_codec import decode_head_bundle, load_head_bundle
from trader.infra.scoring.profiles.v3.bundle_locator import locate_head_bundles
from trader.infra.scoring.profiles.v3.profile import build_scoring_profile
from trader.infra.scoring.profiles.v3.training_bundle_repository import (
    HeadBundlePublicationIdentity,
    inspect_active_head_bundle,
    publish_head_bundle,
    recover_head_bundle_publication,
)
from trader.infra.scoring.profiles.v3.training_contracts import (
    TOMORROW_HEAD_CONTRACT,
    V3_HEAD_CONTRACTS,
    V3HeadTrainingContract,
)


def _model_payload_hash(document: dict[str, object]) -> str:
    return artifact_content_hash(
        {key: value for key, value in document.items() if key not in {"report_hash", "model_payload_hash"}}
    )


def _documents(contract: V3HeadTrainingContract) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    bundled = json.loads(
        resources.files("trader.infra.scoring.profiles.v2").joinpath("model.json").read_text(encoding="utf-8")
    )
    codes = ["600000", "600001"]
    training_input: dict[str, object] = {
        "schema_version": "v3_head_training_input",
        "profile_id": "v3",
        "strategy_head": contract.strategy.value,
        "training_input_scope": "complete_manifest",
        "training_input_hash": "a" * 64,
        "label_cutoff": "2026-09-08",
        "source_identity_hash": "1" * 64,
        "calendar_hash": "4" * 64,
        "source_cutoff": "2026-09-09",
        "requested_sessions": 2000,
        "input_descriptor_hash": "5" * 64,
        "training_input_codes": len(codes),
        "training_universe_codes": len(codes),
        "codes": codes,
        "feature_manifest_hash": contract.feature_manifest.content_hash,
        "split_hash": "b" * 64,
        "training_contract_hash": "e" * 64,
        "label_target": contract.label_target,
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "production_authority": False,
    }
    training_input["content_hash"] = artifact_content_hash(training_input)
    width = len(contract.feature_manifest.names)
    model: dict[str, object] = {
        "schema_version": "v3_head_scoring_model",
        "profile_id": "v3",
        "model_id": contract.model_id,
        "strategy_head": contract.strategy.value,
        "feature_ids": list(contract.feature_manifest.names),
        "feature_units": list(contract.feature_manifest.units),
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
        "training_input_document_hash": training_input["content_hash"],
        "training_input_codes": len(codes),
        "training_universe_codes": len(codes),
        "split_hash": "b" * 64,
        "report_hash": "c" * 64,
        "feature_manifest_hash": contract.feature_manifest.content_hash,
        "training_contract_hash": "e" * 64,
        "label_target": contract.label_target,
        "training_cost_bps": 0,
        "validation_scope": "daily_close_engineering_proxy",
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "model_payload_hash": "f" * 64,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": contract.runtime_anchor,
        "point_in_time_parity": False,
        "training_rows": 20_000,
        "validation_rows": 1_000,
        "industry_count": 1,
        "ensemble_weights": {"ridge": 0.5, "lightgbm": 0.5},
        "industries": {
            "银行": {
                "transformer_means": [0.0] * width,
                "transformer_scales": [1.0] * width,
                "ridge_intercept": 0.0,
                "ridge_coefficients": [0.1] * width,
                "lightgbm_model": bundled["lightgbm_model"],
                "lightgbm_best_iteration": bundled["lightgbm_best_iteration"],
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
    model["model_payload_hash"] = _model_payload_hash(model)
    targets = (
        ("target_t2", "target_t3", "target_t4", "target_t5", "target_d25_aggregate")
        if contract.strategy is Strategy.D25
        else ("target_t1",)
    )
    report: dict[str, object] = {
        "schema_version": "v3_head_training_report",
        "profile_id": "v3",
        "model_id": contract.model_id,
        "strategy_head": contract.strategy.value,
        "training_input_scope": "complete_manifest",
        "training_input_hash": "a" * 64,
        "label_cutoff": "2026-09-08",
        "source_identity_hash": "1" * 64,
        "training_input_document_hash": training_input["content_hash"],
        "training_input_codes": len(codes),
        "training_universe_codes": len(codes),
        "feature_manifest_hash": contract.feature_manifest.content_hash,
        "split_hash": "b" * 64,
        "training_contract_hash": "e" * 64,
        "model_payload_hash": model["model_payload_hash"],
        "label_target": contract.label_target,
        "training_cost_bps": 0,
        "training_anchor": "15:00_close_proxy",
        "runtime_anchor": contract.runtime_anchor,
        "point_in_time_parity": False,
        "validation_scope": "daily_close_engineering_proxy",
        "historical_status": "historical_data_insufficient",
        "historical_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "industry_count": 1,
        "training_rows": 20_000,
        "validation_rows": 1_000,
        "target_metrics": {target: {"count": 1_000, "mean": 0.01, "standard_deviation": 0.02} for target in targets},
        "validation_passed": True,
        "failure_reasons": [],
        "automatic_model_update": False,
        "production_authority": False,
    }
    report["content_hash"] = artifact_content_hash(report)
    model["report_hash"] = report["content_hash"]
    model["model_payload_hash"] = _model_payload_hash(model)
    report["model_payload_hash"] = model["model_payload_hash"]
    report["content_hash"] = artifact_content_hash(
        {key: value for key, value in report.items() if key != "content_hash"}
    )
    model["report_hash"] = report["content_hash"]
    model["content_hash"] = artifact_content_hash(model)
    return model, report, training_input


def _write_bundle(staging: Path, contract: V3HeadTrainingContract) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    model, report, training_input = _documents(contract)
    (staging / "model.json").write_text(json.dumps(model), encoding="utf-8")
    (staging / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (staging / "training-input.json").write_text(json.dumps(training_input), encoding="utf-8")


def _publish(training_root: Path, contract: V3HeadTrainingContract, name: str = "staging") -> Path:
    staging = training_root / name
    _write_bundle(staging, contract)
    return publish_head_bundle(
        staging,
        training_root / contract.directory_name,
        contract.strategy,
        HeadBundlePublicationIdentity("a" * 64, "1" * 64, date(2026, 9, 8)),
    )


def test_v3_publication_uses_only_four_portable_fixed_files(tmp_path: Path) -> None:
    selected = _publish(tmp_path, TOMORROW_HEAD_CONTRACT)

    assert selected == tmp_path / "tomorrow-v3/model.json"
    assert sorted(path.name for path in selected.parent.iterdir()) == [
        "active-bundle.json",
        "model.json",
        "report.json",
        "training-input.json",
    ]
    pointer = json.loads((selected.parent / "active-bundle.json").read_text(encoding="utf-8"))
    assert set(pointer) == {"model_hash", "report_hash", "training_input_hash", "content_hash"}
    assert "source_commit" not in (selected.parent / "model.json").read_text(encoding="utf-8")
    assert not (selected.parent / "generations").exists()


def test_v3_loader_requires_and_builds_three_distinct_heads(tmp_path: Path) -> None:
    for index, contract in enumerate(V3_HEAD_CONTRACTS):
        _publish(tmp_path, contract, f"staging-{index}")

    located = locate_head_bundles(tmp_path)
    profile = load_scoring_profile("v3", training_root=tmp_path)

    assert tuple(strategy for strategy, _ in located) == (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    assert tuple(profile.heads) == (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)
    assert len({head.predictor.model_hash for head in profile.heads.values()}) == 3
    assert profile.heads[Strategy.TODAY].evidence.runtime_anchor == "11:20"
    assert profile.heads[Strategy.TOMORROW].evidence.runtime_anchor == "14:50"
    assert profile.heads[Strategy.D25].evidence.runtime_anchor == "14:50"


def test_v3_loader_fails_closed_when_any_head_is_missing(tmp_path: Path) -> None:
    _publish(tmp_path, TOMORROW_HEAD_CONTRACT)

    with pytest.raises(RuntimeError, match="unavailable"):
        load_scoring_profile("v3", training_root=tmp_path)


def test_v3_tomorrow_predictor_preserves_the_existing_numeric_contract() -> None:
    model, _, _ = _documents(TOMORROW_HEAD_CONTRACT)
    artifact = decode_head_bundle(model, Strategy.TOMORROW)
    profile = build_scoring_profile(
        tuple(decode_head_bundle(_documents(contract)[0], contract.strategy) for contract in V3_HEAD_CONTRACTS)
    )
    predictor = profile.heads[Strategy.TOMORROW].predictor
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行")

    assert artifact.model_id == "industry_ridge_lightgbm"
    assert predictor.predict((row,)) == predictor.predict((row,))
    assert predictor.industry_ids == ("银行",)  # type: ignore[attr-defined]


def test_v3_codec_rejects_cross_head_and_nonportable_fields() -> None:
    model, _, _ = _documents(TOMORROW_HEAD_CONTRACT)
    with pytest.raises(ValueError, match="contract"):
        decode_head_bundle(model, Strategy.TODAY)

    model.pop("content_hash")
    model["source_commit"] = "d" * 40
    model["content_hash"] = artifact_content_hash(model)
    with pytest.raises(ValueError, match="fields"):
        decode_head_bundle(model, Strategy.TOMORROW)


def test_v3_loader_rejects_tampered_group_identity(tmp_path: Path) -> None:
    selected = _publish(tmp_path, TOMORROW_HEAD_CONTRACT)
    report = json.loads(selected.with_name("report.json").read_text(encoding="utf-8"))
    report["training_contract_hash"] = "f" * 64
    report["content_hash"] = artifact_content_hash(
        {key: value for key, value in report.items() if key != "content_hash"}
    )
    selected.with_name("report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="identities"):
        load_head_bundle(selected, Strategy.TOMORROW)


def test_v3_failed_replacement_restores_previous_fixed_group(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    output = tmp_path / TOMORROW_HEAD_CONTRACT.directory_name
    _publish(tmp_path, TOMORROW_HEAD_CONTRACT, "first-staging")
    previous = {
        name: (output / name).read_bytes()
        for name in ("training-input.json", "report.json", "model.json", "active-bundle.json")
    }
    staging = tmp_path / "second-staging"
    _write_bundle(staging, TOMORROW_HEAD_CONTRACT)
    original_replace = os.replace

    def fail_report(source: str | Path, destination: str | Path) -> None:
        if Path(source) == staging / "report.json":
            raise KeyboardInterrupt
        original_replace(source, destination)

    monkeypatch.setattr("trader.infra.scoring.profiles.v3.training_bundle_repository.os.replace", fail_report)
    with pytest.raises(KeyboardInterrupt):
        publish_head_bundle(
            staging,
            output,
            Strategy.TOMORROW,
            HeadBundlePublicationIdentity("a" * 64, "1" * 64, date(2026, 9, 8)),
        )

    assert {name: (output / name).read_bytes() for name in previous} == previous
    assert not (output / ".bundle-publication.json").exists()


def test_flat_publication_recovery_restores_previous_files(tmp_path: Path) -> None:
    output = tmp_path / TOMORROW_HEAD_CONTRACT.directory_name
    _publish(tmp_path, TOMORROW_HEAD_CONTRACT)
    previous = {
        name: (output / name).read_bytes()
        for name in ("training-input.json", "report.json", "model.json", "active-bundle.json")
    }
    rollback = output / ".bundle-rollback.test"
    rollback.mkdir()
    for name, content in previous.items():
        (rollback / name).write_bytes(content)
    (output / "model.json").write_text("{}", encoding="utf-8")
    journal = {
        "schema_version": "v3_head_bundle_publication",
        "strategy_head": "tomorrow",
        "rollback_directory": rollback.name,
        "previous_files": list(previous),
        "new_pointer_hash": "f" * 64,
    }
    journal["content_hash"] = artifact_content_hash(journal)
    (output / ".bundle-publication.json").write_text(json.dumps(journal), encoding="utf-8")

    recover_head_bundle_publication(output, Strategy.TOMORROW)

    assert {name: (output / name).read_bytes() for name in previous} == previous
    assert inspect_active_head_bundle(output, Strategy.TOMORROW).strategy is Strategy.TOMORROW
    assert not rollback.exists()
