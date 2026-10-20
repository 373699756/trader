import sqlite3
from datetime import date, timedelta

import pytest

from trader.download.domain.baostock_daily import BuildBaostockTrainingSplit
from trader.infra.sqlite_connection import OpenSqliteConnection
from trader.training.infra.sample_cache import TrainingSampleCache
from trader.training.infra.sample_index import TrainingSample


def test_complete_cache_reopens_read_only_and_input_change_rebuilds(tmp_path):
    dates = tuple(date(2020, 1, 1) + timedelta(days=index) for index in range(2000))
    split = BuildBaostockTrainingSplit(dates, parent_manifest_hash="a" * 64)
    cache = TrainingSampleCache(tmp_path, "first-input", 1, split.content_hash)
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
    changed = TrainingSampleCache(tmp_path, "changed-input", 1, split.content_hash)
    with changed.open() as samples:
        assert not changed.reused and samples.count() == 0
    changed.clear()
    assert not changed.path.exists()


def test_partial_cache_is_never_reused(tmp_path):
    cache = TrainingSampleCache(tmp_path, "same-input", 1, "split")
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
    cache = TrainingSampleCache(tmp_path, "input", 1, "split")
    with pytest.raises(sqlite3.OperationalError, match="cannot initialize"):
        cache.open()
    assert not cache.manifest.exists()
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].run("SELECT 1")
