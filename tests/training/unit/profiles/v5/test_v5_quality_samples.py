"""Retained V5 quality caches use fixed inputs without adopting model authority."""

from tests.helpers.training.training_resources import TrainingSampleSettingsFixture

from dataclasses import replace
from pathlib import Path
import time

import pytest

from scripts.training.samples.v5_quality_samples import PrepareV5QualitySamples
from tests.helpers.shared.configuration import SqlitePolicyFixture
from tests.helpers.training.profiles.v5.v5_training import _history
from trader.infra.atomic_files.json import AtomicReadJson
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths

ROOT = Path(__file__).resolve().parents[5]


def _inputs(tmp_path, monkeypatch):
    runtime = replace(LoadRuntimeSettings(ROOT / "config/runtime.json"), data_paths=DataPaths(tmp_path / "data"))
    runtime.data_paths.history.parent.mkdir(parents=True)
    history = _history(runtime.data_paths.history)
    monkeypatch.setattr(
        "scripts.training.samples.v5_quality_samples.SQLiteHistoryTrainingInputSnapshot.open",
        lambda _root, *, industry_snapshot, sqlite_policy: history,
    )
    monkeypatch.setattr(
        "scripts.training.samples.v5_quality_samples.ReadIndustrySnapshot", lambda *_args: history.snapshot.industry_snapshot,
    )
    report = tmp_path / "reports"
    report.mkdir()
    return runtime, history, runtime.data_paths.samples / "v5-quality", report


def test_preparation_retains_audited_samples_and_reuses_without_scanning_or_model_publication(tmp_path, monkeypatch):
    runtime, history, samples, report = _inputs(tmp_path, monkeypatch)
    first = PrepareV5QualitySamples(runtime, ROOT, samples, report, time.monotonic() + 60, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert first.rows > 0 and first.source_codes == 6 and first.fit_dates == 61 and not first.reused
    assert first.snapshot_hash == history.snapshot.snapshot_hash
    assert first.input_descriptor_hash == history.snapshot.input_descriptor_hash
    before = (samples / "samples.sqlite3").read_bytes(), (samples / "complete.json").read_bytes()
    assert AtomicReadJson(samples / "complete.json")["identity"] == first.sample_identity
    monkeypatch.setattr(history, "iter_training_rows", lambda *_a, **_k: pytest.fail("sealed inputs were scanned again"))
    second = PrepareV5QualitySamples(runtime, ROOT, samples, report, time.monotonic() + 60, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert second.reused and second.rows == first.rows and second.sample_identity == first.sample_identity
    assert ((samples / "samples.sqlite3").read_bytes(), (samples / "complete.json").read_bytes()) == before
    assert not tuple(runtime.data_paths.models.rglob("*.json"))


def test_unowned_existing_sample_directory_and_changed_inputs_are_preserved(tmp_path, monkeypatch):
    runtime, _history_input, samples, report = _inputs(tmp_path, monkeypatch)
    samples.mkdir(parents=True)
    protected = samples / "notes"
    protected.write_bytes(b"keep")
    with pytest.raises(ValueError, match="existing_sample_identity"):
        PrepareV5QualitySamples(runtime, ROOT, samples, report, time.monotonic() + 60, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert protected.read_bytes() == b"keep" and tuple(samples.iterdir()) == (protected,)


def test_history_change_prevents_sealing_and_expired_budget_never_creates_samples(tmp_path, monkeypatch):
    runtime, history, samples, report = _inputs(tmp_path, monkeypatch)
    with pytest.raises(TimeoutError):
        PrepareV5QualitySamples(runtime, ROOT, samples, report, time.monotonic() - 1, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert not samples.exists()
    original = history.iter_training_rows

    def changed(dates, progress=None):
        yield from original(dates, progress)
        history.snapshot = replace(history.snapshot, snapshot_hash="e" * 64)

    monkeypatch.setattr(history, "iter_training_rows", changed)
    with pytest.raises(ValueError, match="history_changed"):
        PrepareV5QualitySamples(runtime, ROOT, samples, report, time.monotonic() + 60, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert not (samples / "complete.json").exists()


def test_sample_output_must_be_a_child_of_the_configured_samples_directory(tmp_path, monkeypatch):
    runtime, _history_input, _samples, report = _inputs(tmp_path, monkeypatch)
    for root in (runtime.data_paths.samples, tmp_path / "foreign"):
        with pytest.raises(ValueError, match="child_of_configured_samples"):
            PrepareV5QualitySamples(runtime, ROOT, root, report, time.monotonic() + 60, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())


def test_earlier_history_warmup_obeys_the_budget_and_closes_the_source(tmp_path, monkeypatch):
    from scripts.training.samples import v5_quality_samples

    runtime, history, samples, report = _inputs(tmp_path, monkeypatch)
    closed = []
    publish = v5_quality_samples._Progress.publish

    def timeout_on_physical_read(progress, update):
        if update.stage == "input_read":
            progress.deadline = time.monotonic() - 1
        publish(progress, update)

    def rows(dates, progress=None):
        try:
            assert progress is not None
            progress(1)
            yield from ()
        finally:
            closed.append(True)

    monkeypatch.setattr(history, "iter_training_rows", rows)
    monkeypatch.setattr(v5_quality_samples._Progress, "publish", timeout_on_physical_read)
    with pytest.raises(TimeoutError, match="sample_budget_exhausted"):
        PrepareV5QualitySamples(runtime, ROOT, samples, report, time.monotonic() + 60, sqlite_policy=SqlitePolicyFixture(), sample_settings=TrainingSampleSettingsFixture())
    assert closed == [True] and not (samples / "complete.json").exists()
