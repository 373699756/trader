"""Published pairs are atomic at load time and model failures have explicit categories."""

from tests.helpers.configuration import SqlitePolicyFixture

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.unit.training.infra.profile.v3.test_profile import _publish_profile
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
