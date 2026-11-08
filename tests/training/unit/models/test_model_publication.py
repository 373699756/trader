"""Published pairs are atomic at load time and model failures have explicit categories."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.training.profiles.v3.profile import _documents, _hash, _publish_profile, _recent_documents
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.infra.scoring import profile_factory
from trader.recommendation.infra.scoring.profile_factory import LoadScoringProfile, ScoringModelsUnavailableError
from trader.training.infra.history.history_training_due import PublishedTrainingState
from trader.training.infra.model_bundles import profile_publication
from trader.training.infra.model_bundles.profile_publication import (
    InspectPublishedProfileBundles,
    ProfilePublicationBusyError,
    PublishProfileBundles,
)
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.model_bundles.bundle_codec import DecodeHeadBundle
from trader.training.infra.model_evaluation import PREDICTION_EVALUATION_CONTRACT


@pytest.mark.parametrize("failure", ("missing", "corrupt", "integrity_conflict", "contract_conflict"))
def test_loading_classifies_actual_document_failures(tmp_path, failure):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    path = tmp_path / "v3/d25/metadata.json"
    if failure == "missing":
        path.unlink()
    elif failure == "corrupt":
        path.write_text("{", encoding="utf-8")
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        document["model_id"] = "wrong-model"
        if failure == "contract_conflict":
            document.pop("content_hash")
            document["content_hash"] = ContentHash(document)
        path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ScoringModelsUnavailableError) as caught:
        LoadScoringProfile("v3", training_root=tmp_path)
    assert caught.value.reason == failure


def test_unexpected_runtime_error_is_not_reclassified_as_missing_models(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise RuntimeError("programming defect")

    monkeypatch.setattr(profile_factory, "InspectPublishedProfileBundles", unexpected)
    with pytest.raises(RuntimeError, match="programming defect"):
        LoadScoringProfile("v3", training_root=tmp_path)


def _legacy_refit_documents(contract):
    """Generate a 91d-schema fixture directly at the test codec boundary."""
    from datetime import date

    model, metadata = _recent_documents(contract)
    evidence = metadata["final_fit"]
    calendar = tuple(date.fromisoformat(day) for day in evidence["calendar_dates"])
    research = BuildTrainingDateSplit(
        calendar, parent_manifest_hash=metadata["training_input"]["price_snapshot_hash"],
        fit_dates=len(evidence["fit_dates"]), early_stopping_sessions=0, history_sessions=61,
    )
    split_hash = ContentHash({
        "calendar_split_hash": research.content_hash, "strategy_head": contract.strategy.value,
        "maturity_sessions": contract.maturity_sessions, "label_target": contract.label_target,
    })
    metadata["schema_version"] = "v3_head_refit_metadata"
    for field in ("diagnostic_contract", "diagnostic_source_cutoff", "diagnostic_maturity_sessions"):
        evidence.pop(field)
    evidence.update(
        diagnostic_split_hash=research.content_hash,
        diagnostic_dates={
            "fitting": [day.isoformat() for day in research.model_fit_dates],
            "early_stopping": [], "calibration": [day.isoformat() for day in research.calibration_dates],
            "validation": [day.isoformat() for day in research.confirmation_dates],
            "count_only_holdout": [day.isoformat() for day in research.daily_proxy_holdout_dates],
        },
        calibration_policy="frozen_diagnostic_model_transfer",
        research_holdout_status="may_be_used_by_final_refit_not_unseen_for_final_model",
    )
    inputs, report = metadata["training_input"], metadata["validation_report"]
    inputs["split_hash"] = split_hash
    inputs.pop("content_hash")
    _hash(inputs)
    validation_rows = len(research.confirmation_dates) * 2
    report.update(split_hash=split_hash, training_input_document_hash=inputs["content_hash"],
                  validation_rows=validation_rows, evaluation_contract=PREDICTION_EVALUATION_CONTRACT)
    for metric in (report["prediction_metrics"], report["industry_prediction_metrics"]["银行"]):
        metric.update(input_rows=validation_rows, predicted_rows=validation_rows,
                      covered_dates=len(research.confirmation_dates), rank_ic_dates=len(research.confirmation_dates))
    for metric in report["target_metrics"].values():
        metric["count"] = validation_rows
    report["evaluation_coverage"].update(candidate_validation_rows=validation_rows, predicted_rows=validation_rows,
                                       sealed_holdout_rows=len(research.daily_proxy_holdout_dates) * 2,
                                       holdout_status="sealed_rows_count_only")
    report.pop("content_hash")
    _hash(report)
    metadata["industry_sample_counts"]["银行"]["validation_rows"] = validation_rows
    metadata.pop("content_hash")
    _hash(metadata)
    return model, metadata


@pytest.mark.parametrize("existing", (False, True))
@pytest.mark.parametrize("legacy_schema", ("research", "91d", "mixed"))
def test_publisher_rejects_old_staged_models_before_any_directory_write(tmp_path, existing, legacy_schema):
    if existing:
        _publish_profile(tmp_path / "installed", V3_TRAINING_PROFILE)
    root = tmp_path / "installed" / "v3"
    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        recent = legacy_schema == "mixed" and contract.maturity_sessions == 1
        if recent:
            model, metadata = _recent_documents(contract)
        elif legacy_schema == "91d":
            model, metadata = _legacy_refit_documents(contract)
        else:
            model, metadata = _documents(contract)
        decoded = DecodeHeadBundle(model, metadata, contract.strategy, V3_TRAINING_PROFILE)
        assert (decoded.diagnostic_plan is not None) is recent
        path = tmp_path / "staged" / contract.strategy.value
        path.mkdir(parents=True)
        for name, document in (("model.json", model), ("metadata.json", metadata)):
            (path / name).write_text(json.dumps(document), encoding="utf-8")
        staged.append((contract.strategy, path))
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    directories = {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()}

    with pytest.raises(ValueError, match="requires recent production diagnostics"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, tuple(staged))

    assert before == {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert directories == {path.relative_to(tmp_path) for path in tmp_path.rglob("*") if path.is_dir()}
    assert root.exists() is existing


def test_publisher_rechecks_recent_qualification_after_staging_copy(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    before = {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")}
    staged = tuple((contract.strategy, tmp_path / f"staging-v3-{contract.strategy.value}") for contract in V3_TRAINING_PROFILE.heads)
    copyfile = profile_publication.shutil.copyfile
    downgraded = False

    def downgrade_before_copy(source, destination):
        nonlocal downgraded
        if not downgraded:
            downgraded = True
            for contract, (_strategy, path) in zip(V3_TRAINING_PROFILE.heads, staged, strict=True):
                model, metadata = _documents(contract)
                (path / "model.json").write_text(json.dumps(model), encoding="utf-8")
                (path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return copyfile(source, destination)

    monkeypatch.setattr(profile_publication.shutil, "copyfile", downgrade_before_copy)
    with pytest.raises(ValueError, match="requires recent production diagnostics"):
        PublishProfileBundles(root, V3_TRAINING_PROFILE, staged)
    assert before == {path.relative_to(root): path.read_bytes() for path in root.rglob("*.json")}
    assert not any(path.is_dir() for path in tmp_path.glob(".v3.publication.*"))


def test_reader_wait_is_bounded_during_the_directory_switch(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    root = tmp_path / "v3"
    loaded = LoadScoringProfile("v3", training_root=tmp_path)
    original = loaded.publications
    moved, release = threading.Event(), threading.Event()
    rename = profile_publication.os.replace

    def pause_after_old_directory_move(source, target):
        rename(source, target)
        if source == root:
            moved.set()
            assert release.wait(5)

    monkeypatch.setattr(profile_publication.os, "replace", pause_after_old_directory_move)
    with ThreadPoolExecutor(max_workers=1) as executor:
        publication = executor.submit(_publish_profile, tmp_path, V3_TRAINING_PROFILE)
        try:
            assert moved.wait(5)
            started = time.monotonic()
            with pytest.raises(ProfilePublicationBusyError):
                InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE, timeout_seconds=0.03)
            assert time.monotonic() - started < 1
            assert loaded.publications == original
        finally:
            release.set()
        publication.result(timeout=5)
    heads = InspectPublishedProfileBundles(root, V3_TRAINING_PROFILE)
    assert len(heads) == 2
    assert len({head.training_run_hash for head in heads}) == 1
    assert LoadScoringProfile("v3", training_root=tmp_path).publications == original


def test_model_publication_state_is_fixed_until_restart_without_constructing_new_predictors(tmp_path, monkeypatch):
    _publish_profile(tmp_path, V3_TRAINING_PROFILE)
    loaded = LoadScoringProfile("v3", training_root=tmp_path)
    now = datetime(2026, 10, 20, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    projection = PublishedTrainingState(
        tmp_path / "history", tmp_path, loaded.publications, owned_profile="v3", wall_clock=lambda: now, sqlite_policy=SqlitePolicyFixture()
    )
    projection.initialize()
    before = projection.status()
    assert not any(head.restart_required for head in before)
    staged = []
    for contract in V3_TRAINING_PROFILE.heads:
        path = tmp_path / f"staging-v3-{contract.strategy.value}"
        metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
        metadata["dependencies"]["numpy"] = "2.1.0"
        metadata.pop("content_hash")
        metadata["content_hash"] = ContentHash(metadata)
        (path / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        staged.append((contract.strategy, path))
    PublishProfileBundles(tmp_path / "v3", V3_TRAINING_PROFILE, tuple(staged))
    with monkeypatch.context() as scope:
        scope.setattr(profile_factory, "BuildTrainedScoringProfile", lambda *args: pytest.fail("no hot model loading"))
        projection.initialize()
    assert projection.status() == before
    current = tuple(head for head in projection.status() if head.owned)
    assert len(current) == 2 and not any(head.restart_required for head in current)
    assert tuple(head.loaded for head in current) == loaded.publications
    assert tuple(head.disk for head in current) == loaded.publications
    updated = LoadScoringProfile("v3", training_root=tmp_path)
    assert updated.publications != loaded.publications
    restarted = PublishedTrainingState(
        tmp_path / "history", tmp_path, updated.publications, owned_profile="v3", wall_clock=lambda: now, sqlite_policy=SqlitePolicyFixture()
    )
    restarted.initialize()
    assert tuple(head.disk for head in restarted.status() if head.owned) == updated.publications
