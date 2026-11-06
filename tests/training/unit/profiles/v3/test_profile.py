from __future__ import annotations

import json

from dataclasses import replace


from pathlib import Path



import pytest

from trader.infra.serialization.canonical import ContentHash

from trader.recommendation.application.ports.loaded_profile import ModelInput

from trader.recommendation.domain.publication.models import Strategy

from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile

from trader.training.infra.model_bundles.bundle_codec import DecodeHeadBundle, LoadHeadBundle, ModelBundleContractError

from trader.training.infra.model_bundles.contracts import TrainedProfileContract

from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile

from trader.training.infra.model_bundles.profile_publication import (
    InspectPublishedProfileBundles,
    PublishProfileBundles,
)


from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE

from trader.training.infra.profile.v3.contracts import TOMORROW_HEAD_CONTRACT, V3_TRAINING_PROFILE

from tests.helpers.training.profiles.v3.profile import (
    _documents,
    _hash,
    _publish_profile,
    _write_bundle,
)


def test_old_artifact_without_actual_prediction_contract_requires_retraining():
    model, metadata = _documents(TOMORROW_HEAD_CONTRACT)
    report = metadata["validation_report"]
    report.pop("content_hash")
    report.pop("evaluation_contract")
    _hash(report)
    metadata.pop("content_hash")
    _hash(metadata)
    with pytest.raises(ModelBundleContractError, match="fields are invalid"):
        DecodeHeadBundle(model, metadata, Strategy.TOMORROW, V3_TRAINING_PROFILE)


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE))
def test_two_file_pair_loads_both_heads(profile: TrainedProfileContract, tmp_path: Path) -> None:
    _publish_profile(tmp_path, profile)
    root = tmp_path / profile.output_directory
    assert {path.name for path in root.iterdir()} == {"tomorrow", "d25"}
    for strategy in (Strategy.TOMORROW, Strategy.D25):
        assert {path.name for path in (root / strategy.value).iterdir()} == {"model.json", "metadata.json"}
    located = InspectPublishedProfileBundles(root, profile)
    scoring = LoadScoringProfile(profile.profile_id, training_root=tmp_path)
    assert tuple(item.strategy for item in located) == (Strategy.TOMORROW, Strategy.D25)
    assert tuple(scoring.heads) == (Strategy.TOMORROW, Strategy.D25)
    assert len({head.predictor.model_hash for head in scoring.heads.values()}) == 2


@pytest.mark.parametrize("profile", (V2_TRAINING_PROFILE, V3_TRAINING_PROFILE))
@pytest.mark.parametrize("name", ("model.json", "metadata.json"))
def test_missing_or_tampered_file_invalidates_whole_profile(
    profile: TrainedProfileContract, name: str, tmp_path: Path
) -> None:
    _publish_profile(tmp_path, profile)
    path = tmp_path / profile.output_directory / "d25" / name
    original = path.read_bytes()
    path.unlink()
    with pytest.raises(RuntimeError):
        LoadScoringProfile(profile.profile_id, training_root=tmp_path)
    path.write_bytes(original)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError):
        LoadScoringProfile(profile.profile_id, training_root=tmp_path)


def test_pair_with_conflicting_training_run_is_rejected(tmp_path: Path) -> None:
    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        path = tmp_path / contract.strategy.value
        _write_bundle(path, contract)
        staged.append((contract.strategy, path))
    changed = staged[1][1] / "metadata.json"
    metadata = json.loads(changed.read_text(encoding="utf-8"))
    for key in ("training_input", "validation_report"):
        nested = metadata[key]
        nested["training_run_hash"] = "8" * 64
        nested.pop("content_hash")
        nested["content_hash"] = ContentHash(nested)
    metadata["validation_report"]["training_input_document_hash"] = metadata["training_input"]["content_hash"]
    metadata["validation_report"].pop("content_hash")
    metadata["validation_report"]["content_hash"] = ContentHash(metadata["validation_report"])
    metadata.pop("content_hash")
    metadata["content_hash"] = ContentHash(metadata)
    changed.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="different source snapshots"):
        PublishProfileBundles(tmp_path / "v3", V3_TRAINING_PROFILE, tuple(staged))


def test_publication_failure_preserves_previous_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    before = tuple(item.model_hash for item in InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE))
    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        path = tmp_path / f"next-{contract.strategy.value}"
        _write_bundle(path, contract)
        staged.append((contract.strategy, path))
    (staged[1][1] / "metadata.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, tuple(staged))
    assert tuple(item.model_hash for item in InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE)) == before

    _write_bundle(staged[1][1], V3_TRAINING_PROFILE.heads[1])
    from trader.training.infra.model_bundles import profile_publication

    replace = profile_publication.os.replace

    def fail_switch(source: Path, destination: Path) -> None:
        if Path(source).name.startswith(".v3.publication.") and Path(destination) == root:
            raise OSError("fixture_switch_failed")
        replace(source, destination)

    monkeypatch.setattr(profile_publication.os, "replace", fail_switch)
    with pytest.raises(OSError, match="fixture_switch_failed"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, tuple(staged))
    assert tuple(item.model_hash for item in InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE)) == before


def test_predictor_and_codec_use_model_parameters_only() -> None:
    model, metadata = _documents(TOMORROW_HEAD_CONTRACT)
    assert set(model) == {"industries", "content_hash"}
    assert "training_rows" not in model["industries"]["银行"]
    assert set(metadata["industry_sample_counts"]["银行"]) == {"training_rows", "validation_rows"}
    artifacts = tuple(
        DecodeHeadBundle(*_documents(contract), contract.strategy, V3_TRAINING_PROFILE)
        for contract in V3_TRAINING_PROFILE.heads
    )
    predictor = BuildTrainedScoringProfile("v3", artifacts).heads[Strategy.TOMORROW].predictor
    row = ModelInput("600000", (0.01, 0.02, 0.03, 0.01, -0.02, 0.03), "银行")
    assert predictor.predict((row,))[0].predicted_excess_return == pytest.approx(0.008)
    assert artifacts[0].content_hash == metadata["model_hash"]
    assert artifacts[0].metadata_hash == metadata["content_hash"]


def test_cross_head_and_rehashed_contract_tampering_fail() -> None:
    model, metadata = _documents(TOMORROW_HEAD_CONTRACT)
    with pytest.raises(ValueError):
        DecodeHeadBundle(model, metadata, Strategy.D25, V3_TRAINING_PROFILE)
    altered = dict(metadata)
    altered["model_hash"] = "f" * 64
    altered.pop("content_hash")
    altered["content_hash"] = ContentHash(altered)
    with pytest.raises(ValueError, match="contract"):
        DecodeHeadBundle(model, altered, Strategy.TOMORROW, V3_TRAINING_PROFILE)


def test_rehashed_nested_identity_conflict_fails(tmp_path: Path) -> None:
    _write_bundle(tmp_path, TOMORROW_HEAD_CONTRACT)
    path = tmp_path / "metadata.json"
    metadata = json.loads(path.read_text(encoding="utf-8"))
    report = metadata["validation_report"]
    report["training_contract_hash"] = "f" * 64
    report.pop("content_hash")
    report["content_hash"] = ContentHash(report)
    metadata.pop("content_hash")
    metadata["content_hash"] = ContentHash(metadata)
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="contract"):
        LoadHeadBundle(tmp_path / "model.json", Strategy.TOMORROW, V3_TRAINING_PROFILE)


def test_pair_builder_rejects_duplicate_and_conflicting_run() -> None:
    artifacts = tuple(
        DecodeHeadBundle(*_documents(contract), contract.strategy, V3_TRAINING_PROFILE)
        for contract in V3_TRAINING_PROFILE.heads
    )
    with pytest.raises(ValueError, match="require one Tomorrow and D25"):
        BuildTrainedScoringProfile("v3", (*artifacts, artifacts[0]))
    with pytest.raises(ValueError, match="different source snapshots"):
        BuildTrainedScoringProfile("v3", (artifacts[0], replace(artifacts[1], training_run_hash="8" * 64)))
