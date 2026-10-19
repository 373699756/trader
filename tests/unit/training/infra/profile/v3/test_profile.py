from __future__ import annotations

import json
from dataclasses import replace
from functools import lru_cache
from pathlib import Path

import lightgbm as lgb
import numpy as np
import pytest

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile
from trader.training.infra.model_bundles.bundle_codec import DecodeHeadBundle, LoadHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.model_bundles.profile_publication import InspectActiveProfileBundles, PublishProfileBundles
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import (
    HEAD_CONTRACTS,
    TOMORROW_HEAD_CONTRACT,
    V3_TRAINING_PROFILE,
)


def _model_payload_hash(document: dict[str, object]) -> str:
    return ContentHash(
        {key: value for key, value in document.items() if key not in {"report_hash", "model_payload_hash"}}
    )


@lru_cache(maxsize=2)
def _lightgbm_model(width: int) -> str:
    features = np.asarray(
        tuple(tuple(float((row + column) % 3) for column in range(width)) for row in range(12)),
        dtype=np.float64,
    )
    target = np.asarray(tuple(float(row % 2) for row in range(12)), dtype=np.float64)
    booster = lgb.train(
        {
            "objective": "regression",
            "verbosity": -1,
            "num_threads": 1,
            "min_data_in_leaf": 2,
            "num_leaves": 3,
            "seed": 20260913,
        },
        lgb.Dataset(features, label=target),
        num_boost_round=1,
    )
    return booster.model_to_string(num_iteration=1)


def _documents(
    contract: TrainedHeadContract,
    profile: TrainedProfileContract = V3_TRAINING_PROFILE,
) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    codes = ["600000", "600001"]
    training_input: dict[str, object] = {
        "schema_version": f"{profile.profile_id}_head_training_input",
        "profile_id": profile.profile_id,
        "strategy_head": contract.strategy.value,
        "training_input_scope": "complete_manifest",
        "training_input_hash": "a" * 64,
        "training_run_hash": "9" * 64,
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
    training_input["content_hash"] = ContentHash(training_input)
    width = len(contract.feature_manifest.names)
    model_fields: dict[str, object]
    dependencies: dict[str, str]
    if profile.algorithm == "lightgbm":
        model_fields = {
            "lightgbm_model": _lightgbm_model(width),
            "lightgbm_best_iteration": 1,
        }
        dependencies = {"lightgbm": "4.7.0", "numpy": "2.0.0"}
    else:
        model_fields = {
            "ridge_intercept": 0.0,
            "ridge_coefficients": [0.1] * width,
        }
        dependencies = {"numpy": "2.0.0"}
    model: dict[str, object] = {
        "schema_version": f"{profile.profile_id}_head_scoring_model",
        "profile_id": profile.profile_id,
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
        "training_run_hash": "9" * 64,
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
        "algorithm": profile.algorithm,
        "industries": {
            "银行": {
                "transformer_means": [0.0] * width,
                "transformer_scales": [1.0] * width,
                **model_fields,
                "calibration_intercept": 0.0,
                "calibration_slope": 1.0,
                "training_rows": 20_000,
                "validation_rows": 1_000,
            }
        },
        "dependencies": dependencies,
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
        "schema_version": f"{profile.profile_id}_head_training_report_validation_split",
        "profile_id": profile.profile_id,
        "model_id": contract.model_id,
        "strategy_head": contract.strategy.value,
        "training_input_scope": "complete_manifest",
        "training_input_hash": "a" * 64,
        "training_run_hash": "9" * 64,
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
        "artifact_structure_valid": True,
        "artifact_structure_failure_reasons": [],
        "prediction_return_validation_passed": False,
        "prediction_return_failure_reasons": ["daily_close_proxy_not_point_in_time"],
        "automatic_model_update": False,
        "production_authority": False,
    }
    report["content_hash"] = ContentHash(report)
    model["report_hash"] = report["content_hash"]
    model["model_payload_hash"] = _model_payload_hash(model)
    report["model_payload_hash"] = model["model_payload_hash"]
    report["content_hash"] = ContentHash({key: value for key, value in report.items() if key != "content_hash"})
    model["report_hash"] = report["content_hash"]
    model["content_hash"] = ContentHash(model)
    return model, report, training_input


def _write_bundle(
    staging: Path,
    contract: TrainedHeadContract,
    profile: TrainedProfileContract = V3_TRAINING_PROFILE,
) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    model, report, training_input = _documents(contract, profile)
    (staging / "model.json").write_text(json.dumps(model), encoding="utf-8")
    (staging / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (staging / "training-input.json").write_text(json.dumps(training_input), encoding="utf-8")


def _publish_profile(training_root: Path, profile: TrainedProfileContract) -> None:
    staged = []
    for contract in profile.heads:
        staging = training_root / f"staging-{profile.profile_id}-{contract.strategy.value}"
        _write_bundle(staging, contract, profile)
        staged.append((contract.strategy, staging))
    PublishProfileBundles(training_root / profile.output_directory, profile, tuple(staged))


def test_profile_pair_pointer_keeps_old_files_when_second_head_or_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_root = tmp_path / "v3"
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    old_pointer = (profile_root / "active-profile.json").read_bytes()
    old_hashes = tuple(item.model_hash for item in InspectActiveProfileBundles(profile_root, V3_TRAINING_PROFILE))
    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        staging = tmp_path / f"bad-{contract.strategy.value}"
        _write_bundle(staging, contract)
        staged.append((contract.strategy, staging))
    (staged[1][1] / "report.json").write_text("{}", encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        PublishProfileBundles(profile_root, V3_TRAINING_PROFILE, tuple(staged))
    assert (profile_root / "active-profile.json").read_bytes() == old_pointer

    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        staging = tmp_path / f"interrupted-{contract.strategy.value}"
        _write_bundle(staging, contract)
        staged.append((contract.strategy, staging))
    from trader.training.infra.model_bundles import profile_publication

    real_write = profile_publication.AtomicWriteJson

    def fail_commit(path: Path, payload: object) -> None:
        if path.name == "active-profile.json":
            assert (
                tuple(item.model_hash for item in InspectActiveProfileBundles(profile_root, V3_TRAINING_PROFILE))
                == old_hashes
            )
            raise OSError("fixture_pointer_write_failed")
        real_write(path, payload)

    monkeypatch.setattr(profile_publication, "AtomicWriteJson", fail_commit)
    with pytest.raises(OSError, match="fixture_pointer_write_failed"):
        PublishProfileBundles(profile_root, V3_TRAINING_PROFILE, tuple(staged))
    assert (profile_root / "active-profile.json").read_bytes() == old_pointer
    assert (
        tuple(item.model_hash for item in InspectActiveProfileBundles(profile_root, V3_TRAINING_PROFILE)) == old_hashes
    )


def test_profile_directory_switch_failure_restores_the_previous_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    profile_root = tmp_path / "v3"
    old_pointer = (profile_root / "active-profile.json").read_bytes()
    old_hashes = tuple(item.model_hash for item in InspectActiveProfileBundles(profile_root, V3_TRAINING_PROFILE))
    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        staging = tmp_path / f"replacement-{contract.strategy.value}"
        _write_bundle(staging, contract)
        staged.append((contract.strategy, staging))

    from trader.training.infra.model_bundles import profile_publication

    real_replace = profile_publication.os.replace

    def fail_switch(source: Path, destination: Path) -> None:
        if Path(source).name.startswith(".v3.publication.") and Path(destination) == profile_root:
            raise OSError("fixture_directory_switch_failed")
        real_replace(source, destination)

    monkeypatch.setattr(profile_publication.os, "replace", fail_switch)
    with pytest.raises(OSError, match="fixture_directory_switch_failed"):
        PublishProfileBundles(profile_root, V3_TRAINING_PROFILE, tuple(staged))
    assert (profile_root / "active-profile.json").read_bytes() == old_pointer
    assert (
        tuple(item.model_hash for item in InspectActiveProfileBundles(profile_root, V3_TRAINING_PROFILE)) == old_hashes
    )


def test_v3_publication_uses_only_four_portable_fixed_files(tmp_path: Path) -> None:
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    selected = InspectActiveProfileBundles(tmp_path / "v3", V3_TRAINING_PROFILE)[0].model_path

    assert selected.parent.name == "tomorrow"
    assert selected.parent.parent == tmp_path / "v3"
    assert not any(path.name.startswith(".profile-bundle.") for path in selected.parent.parent.iterdir())
    assert sorted(path.name for path in selected.parent.iterdir()) == [
        "active-bundle.json",
        "model.json",
        "report.json",
        "training-input.json",
    ]
    pointer = json.loads((selected.parent / "active-bundle.json").read_text(encoding="utf-8"))
    assert set(pointer) == {"model_hash", "report_hash", "training_input_hash", "content_hash"}
    assert "source_commit" not in (selected.parent / "model.json").read_text(encoding="utf-8")
    assert (tmp_path / "v3" / "active-profile.json").is_file()


def test_shared_loader_builds_two_distinct_heads_for_v2_and_v3(tmp_path: Path) -> None:
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    _publish_profile(tmp_path, V2_TRAINING_PROFILE)

    located = InspectActiveProfileBundles(tmp_path / "v3", V3_TRAINING_PROFILE)
    profiles = tuple(LoadScoringProfile(profile, training_root=tmp_path) for profile in ("v2", "v3"))

    assert tuple(item.strategy for item in located) == (Strategy.TOMORROW, Strategy.D25)
    for profile in profiles:
        assert tuple(profile.heads) == (Strategy.TOMORROW, Strategy.D25)
        assert len({head.predictor.model_hash for head in profile.heads.values()}) == 2
        assert profile.heads[Strategy.TOMORROW].evidence.runtime_anchor == "15:00"
        assert profile.heads[Strategy.D25].evidence.runtime_anchor == "15:00"
    assert (
        tuple(
            item.model_path.parent.parent.name
            for item in InspectActiveProfileBundles(tmp_path / "v2", V2_TRAINING_PROFILE)
        )
        == ("v2",) * 2
    )
    assert tuple(item.model_path.parent.parent.name for item in located) == ("v3",) * 2
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        v2_predictor = profiles[0].heads[strategy].predictor
        v3_predictor = profiles[1].heads[strategy].predictor
        assert v2_predictor.profile_id == "v2"
        assert v3_predictor.profile_id == "v3"
        assert v2_predictor.model_hash != v3_predictor.model_hash
        assert v2_predictor.model_id != v3_predictor.model_id
        assert v2_predictor.feature_ids != v3_predictor.feature_ids


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_shared_loader_fails_closed_for_both_profiles_when_any_head_is_missing(tmp_path: Path, profile: str) -> None:
    selected = V2_TRAINING_PROFILE if profile == "v2" else V3_TRAINING_PROFILE
    _write_bundle(tmp_path / selected.output_directory / "tomorrow", selected.heads[0], selected)

    with pytest.raises(RuntimeError, match="unavailable"):
        LoadScoringProfile(profile, training_root=tmp_path)


@pytest.mark.parametrize("profile", ("v2", "v3"))
def test_shared_loader_fails_closed_for_both_profiles_when_a_bundle_is_corrupt(tmp_path: Path, profile: str) -> None:
    selected = V2_TRAINING_PROFILE if profile == "v2" else V3_TRAINING_PROFILE
    _publish_profile(tmp_path, selected)
    report_path = (
        InspectActiveProfileBundles(tmp_path / selected.output_directory, selected)[0].model_path.parent / "report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["training_contract_hash"] = "f" * 64
    report["content_hash"] = ContentHash({key: value for key, value in report.items() if key != "content_hash"})
    report_path.write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid"):
        LoadScoringProfile(profile, training_root=tmp_path)


def test_shared_tomorrow_predictor_preserves_the_existing_numeric_contract() -> None:
    model, _, _ = _documents(TOMORROW_HEAD_CONTRACT)
    artifact = DecodeHeadBundle(model, Strategy.TOMORROW, V3_TRAINING_PROFILE)
    profile = BuildTrainedScoringProfile(
        "v3",
        tuple(
            DecodeHeadBundle(_documents(contract)[0], contract.strategy, V3_TRAINING_PROFILE)
            for contract in HEAD_CONTRACTS
        ),
    )
    predictor = profile.heads[Strategy.TOMORROW].predictor
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行")

    assert artifact.model_id == "industry_ridge"
    assert predictor.predict((row,)) == predictor.predict((row,))
    assert predictor.industry_ids == ("银行",)  # type: ignore[attr-defined]


def test_shared_profile_rejects_a_duplicate_head_even_when_all_strategies_exist() -> None:
    artifacts = tuple(
        DecodeHeadBundle(_documents(contract)[0], contract.strategy, V3_TRAINING_PROFILE) for contract in HEAD_CONTRACTS
    )

    with pytest.raises(ValueError, match="require one Tomorrow and D25"):
        BuildTrainedScoringProfile("v2", (*artifacts, artifacts[0]))


def test_shared_profile_rejects_heads_from_distinct_runs_on_the_same_snapshot() -> None:
    tomorrow, d25 = tuple(
        DecodeHeadBundle(_documents(contract)[0], contract.strategy, V3_TRAINING_PROFILE) for contract in HEAD_CONTRACTS
    )

    with pytest.raises(ValueError, match="different source snapshots"):
        BuildTrainedScoringProfile("v3", (tomorrow, replace(d25, training_run_hash="8" * 64)))


def test_v3_codec_rejects_cross_head_and_nonportable_fields() -> None:
    model, _, _ = _documents(TOMORROW_HEAD_CONTRACT)
    with pytest.raises(ValueError, match="contract"):
        DecodeHeadBundle(model, Strategy.D25, V3_TRAINING_PROFILE)

    model.pop("content_hash")
    model["source_commit"] = "d" * 40
    model["content_hash"] = ContentHash(model)
    with pytest.raises(ValueError, match="fields"):
        DecodeHeadBundle(model, Strategy.TOMORROW, V3_TRAINING_PROFILE)


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE))
@pytest.mark.parametrize("strategy", (Strategy.TOMORROW, Strategy.D25))
def test_model_codec_rejects_a_rehashed_retired_runtime_anchor(profile, strategy) -> None:
    contract = profile.head_for_strategy(strategy)
    model, _, _ = _documents(contract, profile)
    model.pop("content_hash")
    model["runtime_anchor"] = "14:50"
    model["content_hash"] = ContentHash(model)

    with pytest.raises(ValueError, match="contract"):
        DecodeHeadBundle(model, strategy, profile)


def test_v3_loader_rejects_tampered_group_identity(tmp_path: Path) -> None:
    selected = tmp_path / "model.json"
    _write_bundle(tmp_path, TOMORROW_HEAD_CONTRACT)
    report = json.loads(selected.with_name("report.json").read_text(encoding="utf-8"))
    report["training_contract_hash"] = "f" * 64
    report["content_hash"] = ContentHash({key: value for key, value in report.items() if key != "content_hash"})
    selected.with_name("report.json").write_text(json.dumps(report), encoding="utf-8")

    with pytest.raises(ValueError, match="identities"):
        LoadHeadBundle(selected, Strategy.TOMORROW, V3_TRAINING_PROFILE)


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE))
def test_shared_loader_rejects_retired_report_schema(
    tmp_path: Path,
    profile: TrainedProfileContract,
) -> None:
    contract = profile.head_for_strategy(Strategy.TOMORROW)
    model, report, training_input = _documents(contract, profile)
    report.pop("content_hash")
    report["schema_version"] = f"{profile.profile_id}_head_training_report"
    report.pop("artifact_structure_valid")
    report.pop("artifact_structure_failure_reasons")
    report.pop("prediction_return_validation_passed")
    report.pop("prediction_return_failure_reasons")
    report["validation_passed"] = True
    report["failure_reasons"] = []
    report["content_hash"] = ContentHash(report)
    model["report_hash"] = report["content_hash"]
    model["content_hash"] = ContentHash({key: value for key, value in model.items() if key != "content_hash"})
    for name, document in (("model.json", model), ("report.json", report), ("training-input.json", training_input)):
        (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match="fields or content hash are invalid"):
        LoadHeadBundle(tmp_path / "model.json", Strategy.TOMORROW, profile)
