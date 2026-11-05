
from tests.helpers.configuration import SqlitePolicyFixture
import os
import sqlite3
from datetime import date, timedelta
from multiprocessing import get_context
from pathlib import Path

import pytest

from trader.download.infra.history_control_index import HistoryMaintenanceLock
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteSession
from trader.training.domain.training_dates import BuildTrainingDateSplit, TrainingDateSplit
from trader.training.infra.sample_cache import PruneEmptySampleDirectories, TrainingSampleCache
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample
from trader.training.infra.sample_schema import TrainingFeatureNames
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


def test_read_only_sample_contract_rejects_wrong_features_split_and_old_schema(tmp_path):
    split = BuildTrainingDateSplit(
        tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000)), parent_manifest_hash="a" * 64
    )
    path = tmp_path / "samples.sqlite3"
    with SQLiteTrainingSampleIndex(path, ("first", "second"), sqlite_policy=SqlitePolicyFixture()) as samples:
        samples.prepare_for_model_fitting(split)
    original = path.read_bytes()
    for names, identity in ((("second", "first"), split.content_hash), (("first", "second"), "b" * 64)):
        with pytest.raises(ValueError, match="contract"):
            SQLiteTrainingSampleIndex(path, names, prepared_split_hash=identity, sqlite_policy=SqlitePolicyFixture())
        assert path.read_bytes() == original
    with SqliteSession(path) as connection:
        connection.run("PRAGMA user_version=1")
    original = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        SQLiteTrainingSampleIndex(path, ("first", "second"), prepared_split_hash=split.content_hash, sqlite_policy=SqlitePolicyFixture())
    assert path.read_bytes() == original


def test_complete_cache_reopens_read_only_and_input_change_rebuilds(tmp_path):
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    cache = TrainingSampleCache(tmp_path, "first-input", ("signal",), split.content_hash, sqlite_policy=SqlitePolicyFixture())
    with cache.open() as samples:
        samples.add_final(
            (
                TrainingSample(
                    "600001", split.model_fit_dates[0], "main", "bank", 1.0, (1.0,), (0.1, None, None, None, None, None)
                ),
            )
        )
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    with cache.open() as samples:
        assert cache.reused and samples.count() == 1
        samples.require_split(split)
        assert tuple(samples._connection.run("PRAGMA mmap_size").fetchone()) == (0,)
        assert tuple(samples._connection.run("PRAGMA temp_store").fetchone()) == (1,)
    for name, filename in (
        ("sample-fit-abcdefgh", "training-features.bin"),
        ("sample-shared-abcdefgh", "common-features.bin"),
    ):
        workspace = tmp_path / name
        workspace.mkdir()
        (workspace / filename).write_bytes(b"scratch")
    unrelated = tmp_path / "preserved"
    unrelated.mkdir()
    with cache.open() as samples:
        assert cache.reused and samples.count() == 1
    assert not tuple(tmp_path.glob("sample-fit-*")) and not tuple(tmp_path.glob("sample-shared-*"))
    assert unrelated.is_dir()
    with cache.path.open("ab") as handle:
        handle.write(b"corrupted-tail")
    with cache.open() as samples:
        assert not cache.reused and samples.count() == 0
    changed = TrainingSampleCache(tmp_path, "changed-input", ("signal",), split.content_hash, sqlite_policy=SqlitePolicyFixture())
    with changed.open() as samples:
        assert not changed.reused and samples.count() == 0
    changed.clear()
    assert not changed.path.exists()


def test_normal_reuse_skips_digest_but_explicit_audit_reads_content(tmp_path, monkeypatch):
    split = BuildTrainingDateSplit(tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000)), parent_manifest_hash="a" * 64)
    cache = TrainingSampleCache(tmp_path, "input", ("signal",), split.content_hash, sqlite_policy=SqlitePolicyFixture())
    with cache.open() as samples:
        samples.prepare_for_model_fitting(split)
        with pytest.raises(RuntimeError, match="sealed"):
            samples.add_final(())
        cache.seal(samples)
    calls = []
    monkeypatch.setattr("trader.training.infra.sample_cache._digest", lambda *args: calls.append(args) or "0" * 64)
    reused = TrainingSampleCache(tmp_path, "input", ("signal",), split.content_hash, sqlite_policy=SqlitePolicyFixture())
    assert reused.inspect() and reused.inspect()
    assert calls == []
    assert not reused.audit()
    assert len(calls) == 1


def test_partial_cache_is_never_reused(tmp_path):
    cache = TrainingSampleCache(tmp_path, "same-input", ("signal",), "split", sqlite_policy=SqlitePolicyFixture())
    with cache.open():
        pass
    with cache.open() as samples:
        assert not cache.reused and samples.count() == 0


def test_failed_sample_initialization_closes_sqlite_connection(tmp_path, monkeypatch):
    connections = []

    def failing_connect(*args, **kwargs):
        connection = OpenSqliteConnection(*args, **kwargs)
        connections.append(connection)
        return connection

    def fail_schema(*args, **kwargs):
        raise sqlite3.OperationalError("fixture cannot initialize database")

    monkeypatch.setattr("trader.training.infra.sample_index.OpenSqliteConnection", failing_connect)
    monkeypatch.setattr("trader.training.infra.sample_index.EnsureSqliteSchema", fail_schema)
    cache = TrainingSampleCache(tmp_path, "input", ("signal",), "split", sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(sqlite3.OperationalError, match="cannot initialize"):
        cache.open()
    assert not cache.manifest.exists()
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].run("SELECT 1")


def test_empty_sample_directories_are_pruned_without_deleting_retained_cache(tmp_path):
    samples_root = tmp_path / "samples"
    v2_root = samples_root / "v2"
    v3_root = samples_root / "v3"
    v2_root.mkdir(parents=True)
    v3_root.mkdir()
    retained = v3_root / "samples.sqlite3"
    retained.write_bytes(b"retained")

    PruneEmptySampleDirectories(v2_root, samples_root)
    PruneEmptySampleDirectories(v3_root, samples_root)
    assert not v2_root.exists()
    assert retained.read_bytes() == b"retained"

    retained.unlink()
    PruneEmptySampleDirectories(v3_root, samples_root)
    assert not samples_root.exists()


def test_cache_reports_separate_byte_progress_and_cancelled_seal_is_not_reusable(tmp_path):
    split = BuildTrainingDateSplit(
        tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000)), parent_manifest_hash="a" * 64
    )
    events = []
    cache = TrainingSampleCache(tmp_path, "input", ("signal",), split.content_hash, progress=lambda *event: events.append(event), sqlite_policy=SqlitePolicyFixture())
    with cache.open() as samples:
        samples.add_final(
            (
                TrainingSample(
                    "600001", split.model_fit_dates[0], "main", "bank", 1.0, (1.0,), (0.1, None, None, None, None, None)
                ),
            )
        )
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    assert cache.inspect()
    for phase in ("checking", "sealing"):
        selected = [item for item in events if item[0] == phase]
        size = cache.path.stat().st_size if phase == "sealing" else 1
        assert selected[0] == (phase, "started", 0, size)
        assert selected[-1] == (phase, "completed", size, size)
        assert all(0 <= done <= total == size for _, _, done, total in selected)

    def cancel(phase, state, done, total):
        if phase == "sealing" and state == "running":
            raise InterruptedError("cancelled")

    interrupted = TrainingSampleCache(tmp_path, "new-input", ("signal",), split.content_hash, progress=cancel, sqlite_policy=SqlitePolicyFixture())
    with interrupted.open() as samples:
        samples.prepare_for_model_fitting(split)
        with pytest.raises(InterruptedError):
            interrupted.seal(samples)
    assert not interrupted.manifest.exists()
    assert not interrupted.inspect()


def _matrix_cache(root: Path) -> tuple[TrainingSampleCache, TrainingDateSplit]:
    split = BuildTrainingDateSplit(
        tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000)), parent_manifest_hash="a" * 64,
    )
    return (
        TrainingSampleCache(
            root,
            "matrix-fixture",
            TrainingFeatureNames(V3_TRAINING_PROFILE.heads),
            split.content_hash,
            sqlite_policy=SqlitePolicyFixture(),
        ),
        split,
    )


def _sealed_matrix_cache(root: Path) -> TrainingSampleCache:
    cache, split = _matrix_cache(root)
    features = TrainingFeatureNames(V3_TRAINING_PROFILE.heads)
    with cache.open() as samples:
        samples.add_final(
            TrainingSample(
                "600001",
                day,
                "main",
                "bank",
                1e8,
                tuple(float(index) for index in range(len(features))),
                (0.1,) * 6,
            )
            for day in (split.model_fit_dates[0], split.early_stopping_dates[0], split.calibration_dates[0])
        )
        samples.prepare_for_model_fitting(split)
        cache.seal(samples)
    assert cache.inspect()
    return cache


def _crash_fitting_mapping(root: Path, kind: str) -> None:
    from trader.training.infra import sample_index

    sample_index.TRAINING_MATRIX_BYTES = 0
    cache, _split = _matrix_cache(root)
    contract = V3_TRAINING_PROFILE.heads[0]
    with HistoryMaintenanceLock(root.parent.parent / "models/.v3.training.lock"):
        with cache.open() as samples:
            context = samples.share_industry("bank") if kind == "shared" else samples.industry_data(
                samples.industry_counts(contract)[0], contract,
            )
            with context:
                os._exit(86)


@pytest.mark.parametrize("kind", ("fit", "shared"))
def test_hard_exit_fitting_mappings_release_lock_and_preserve_sealed_samples(tmp_path, kind):
    root = tmp_path / "samples/v3"
    cache = _sealed_matrix_cache(root)
    before = cache.path.read_bytes(), cache.manifest.read_bytes()
    process = get_context("spawn").Process(target=_crash_fitting_mapping, args=(root, kind))
    process.start()
    try:
        process.join(30)
        assert process.exitcode == 86
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)
        process.close()
    residue = tuple(root.glob(f"sample-{kind}-*"))
    assert len(residue) == 1 and tuple(residue[0].glob("*.bin"))
    with HistoryMaintenanceLock(tmp_path / "models/.v3.training.lock"):
        with cache.open() as samples:
            assert cache.reused and samples.count() == 3
    assert not tuple(root.glob(f"sample-{kind}-*"))
    assert (cache.path.read_bytes(), cache.manifest.read_bytes()) == before


@pytest.mark.parametrize("kind", ("fit", "shared"))
@pytest.mark.parametrize("partial", (False, True))
def test_recovery_accepts_empty_and_partially_created_matrix_workspaces(tmp_path, kind, partial):
    cache = _sealed_matrix_cache(tmp_path / "samples/v3")
    before = cache.path.read_bytes(), cache.manifest.read_bytes()
    workspace = cache.root / f"sample-{kind}-abcdefgh"
    workspace.mkdir()
    if partial:
        filename = "training-features.bin" if kind == "fit" else "common-labels.bin"
        (workspace / filename).write_bytes(b"partial mapping")
    with cache.open() as samples:
        assert cache.reused and samples.count() == 3
    assert not workspace.exists()
    assert (cache.path.read_bytes(), cache.manifest.read_bytes()) == before


@pytest.mark.parametrize("kind", ("fit", "shared"))
@pytest.mark.parametrize("foreign", ("file", "directory", "symlink", "workspace_file", "workspace_symlink", "name"))
def test_matrix_recovery_rejects_unowned_objects_without_changing_the_sealed_cache(tmp_path, kind, foreign):
    cache = _sealed_matrix_cache(tmp_path / "samples/v3")
    before = cache.path.read_bytes(), cache.manifest.read_bytes()
    workspace = cache.root / f"sample-{kind}-abcdefgh"
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep")
    if foreign == "workspace_file":
        workspace.write_bytes(b"unowned workspace")
    elif foreign == "workspace_symlink":
        workspace.symlink_to(protected)
    else:
        if foreign == "name":
            workspace = cache.root / f"sample-{kind}-user-notes"
        workspace.mkdir()
        filename = "training-features.bin" if kind == "fit" else "common-features.bin"
        if foreign == "name":
            (workspace / filename).write_bytes(b"keep")
        elif foreign == "file":
            (workspace / "notes").write_bytes(b"keep")
        elif foreign == "directory":
            (workspace / filename).mkdir()
            (workspace / filename / "notes").write_bytes(b"keep")
        else:
            (workspace / filename).symlink_to(protected)
    with pytest.raises(ValueError, match="unknown_matrix_workspace"):
        cache.open()
    assert workspace.exists() and protected.read_bytes() == b"keep"
    assert (cache.path.read_bytes(), cache.manifest.read_bytes()) == before


@pytest.mark.parametrize("invalid", ("fit", "shared", "tcn"))
def test_all_fitting_namespaces_are_checked_before_any_mapping_is_removed(tmp_path, invalid):
    cache = _sealed_matrix_cache(tmp_path / "samples/v3")
    before = cache.path.read_bytes(), cache.manifest.read_bytes()
    paths = (
        ("fit", "sample-fit-abcdefgh", "training-features.bin"),
        ("shared", "sample-shared-abcdefgh", "common-features.bin"),
        ("tcn", "tcn-batches-owned", "training"),
    )
    for kind, name, filename in paths:
        workspace = cache.root / name
        workspace.mkdir()
        (workspace / filename).write_bytes(b"owned scratch")
        if kind == invalid:
            (workspace / "notes").write_bytes(b"keep")
    with pytest.raises(ValueError, match="unknown_.*workspace"):
        cache.open()
    assert all((cache.root / name / filename).read_bytes() == b"owned scratch" for _kind, name, filename in paths)
    assert (cache.path.read_bytes(), cache.manifest.read_bytes()) == before


@pytest.mark.parametrize("filename", ("samples.sqlite3", "samples.sqlite3-journal"))
@pytest.mark.parametrize("foreign", ("symlink", "directory"))
def test_clear_validates_every_fixed_file_before_removing_the_manifest(tmp_path, filename, foreign):
    cache = _sealed_matrix_cache(tmp_path / "samples/v3")
    manifest = cache.manifest.read_bytes()
    path = cache.root / filename
    outside = tmp_path / "outside"
    if path.exists():
        original = path.read_bytes()
        path.rename(outside)
    else:
        original = b"keep"
        outside.write_bytes(original)
    if foreign == "symlink":
        path.symlink_to(outside)
    else:
        path.mkdir()
        (path / "notes").write_bytes(b"keep")
    with pytest.raises(ValueError, match="training_sample_cache_(symlink|unknown_file)"):
        cache.clear()
    assert cache.manifest.read_bytes() == manifest and cache.reused
    assert path.exists() and outside.read_bytes() == original


@pytest.mark.parametrize("foreign", ("symlink", "directory"))
def test_open_rejects_an_invalid_journal_before_removing_fitting_residue(tmp_path, foreign):
    cache = _sealed_matrix_cache(tmp_path / "samples/v3")
    before = cache.path.read_bytes(), cache.manifest.read_bytes()
    scratch = cache.root / "sample-fit-abcdefgh"
    scratch.mkdir()
    (scratch / "training-features.bin").write_bytes(b"owned scratch")
    journal = cache.root / "samples.sqlite3-journal"
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep")
    if foreign == "symlink":
        journal.symlink_to(protected)
    else:
        journal.mkdir()
    with pytest.raises(ValueError, match="training_sample_cache_(symlink|unknown_file)"):
        cache.open()
    assert (scratch / "training-features.bin").read_bytes() == b"owned scratch"
    assert protected.read_bytes() == b"keep"
    assert (cache.path.read_bytes(), cache.manifest.read_bytes()) == before
