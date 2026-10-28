import sqlite3
from datetime import date, timedelta

import pytest

from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteSession
from trader.training.domain.training_dates import BuildTrainingDateSplit
from trader.training.infra.sample_cache import PruneEmptySampleDirectories, TrainingSampleCache
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSample


def test_read_only_sample_contract_rejects_wrong_features_split_and_old_schema(tmp_path):
    split = BuildTrainingDateSplit(
        tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000)), parent_manifest_hash="a" * 64
    )
    path = tmp_path / "samples.sqlite3"
    with SQLiteTrainingSampleIndex(path, ("first", "second")) as samples:
        samples.prepare_for_model_fitting(split)
    original = path.read_bytes()
    for names, identity in ((("second", "first"), split.content_hash), (("first", "second"), "b" * 64)):
        with pytest.raises(ValueError, match="contract"):
            SQLiteTrainingSampleIndex(path, names, prepared_split_hash=identity)
        assert path.read_bytes() == original
    with SqliteSession(path) as connection:
        connection.run("PRAGMA user_version=1")
    original = path.read_bytes()
    with pytest.raises(sqlite3.DatabaseError, match="schema_unsupported"):
        SQLiteTrainingSampleIndex(path, ("first", "second"), prepared_split_hash=split.content_hash)
    assert path.read_bytes() == original


def test_complete_cache_reopens_read_only_and_input_change_rebuilds(tmp_path):
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000))
    split = BuildTrainingDateSplit(dates, parent_manifest_hash="a" * 64)
    cache = TrainingSampleCache(tmp_path, "first-input", ("signal",), split.content_hash)
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
    with cache.path.open("ab") as handle:
        handle.write(b"corrupted-tail")
    with cache.open() as samples:
        assert not cache.reused and samples.count() == 0
    changed = TrainingSampleCache(tmp_path, "changed-input", ("signal",), split.content_hash)
    with changed.open() as samples:
        assert not changed.reused and samples.count() == 0
    changed.clear()
    assert not changed.path.exists()


def test_partial_cache_is_never_reused(tmp_path):
    cache = TrainingSampleCache(tmp_path, "same-input", ("signal",), "split")
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
    cache = TrainingSampleCache(tmp_path, "input", ("signal",), "split")
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
    cache = TrainingSampleCache(tmp_path, "input", ("signal",), split.content_hash, progress=lambda *event: events.append(event))
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
        size = cache.path.stat().st_size
        assert selected[0] == (phase, "started", 0, size)
        assert selected[-1] == (phase, "completed", size, size)
        assert all(0 <= done <= total == size for _, _, done, total in selected)

    def cancel(phase, state, done, total):
        if phase == "sealing" and state == "running":
            raise InterruptedError("cancelled")

    interrupted = TrainingSampleCache(tmp_path, "new-input", ("signal",), split.content_hash, progress=cancel)
    with interrupted.open() as samples:
        samples.prepare_for_model_fitting(split)
        with pytest.raises(InterruptedError):
            interrupted.seal(samples)
    assert not interrupted.manifest.exists()
    assert not interrupted.inspect()
