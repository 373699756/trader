"""A cached coverage audit uses narrow statistics and reports conservative label bounds."""

import pytest

from scripts.runtime_diagnostics.training_coverage import _audit
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def test_statistics_audit_does_not_read_wide_samples_and_separates_projection(tmp_path):
    path = tmp_path / "samples.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample_split_dates (trade_date INTEGER, split_name TEXT)")
        connection.run("CREATE TABLE sample_statistics (trade_date INTEGER, target TEXT, sample_count INTEGER)")
        connection.run(
            "CREATE TABLE sample_day_diagnostics (trade_date INTEGER, candidate_count INTEGER, "
            "missing_t1 INTEGER, missing_t2 INTEGER, missing_t3 INTEGER, missing_t4 INTEGER, missing_t5 INTEGER)"
        )
        connection.run_many(
            "INSERT INTO sample_split_dates VALUES (?,?)",
            (
                (20458, "training"),
                (20459, "training"),
                (20460, "training"),
            ),
        )
        connection.run("INSERT INTO sample_statistics VALUES (20458, 'rows', 5)")
        connection.run("INSERT INTO sample_statistics VALUES (20458, 'target_t1', 5)")
        connection.run("INSERT INTO sample_day_diagnostics VALUES (20458, 5, 0, 1, 1, 0, 0)")
        connection.run("INSERT INTO sample_day_diagnostics VALUES (20459, 5, 5, 5, 5, 5, 5)")
        connection.run("PRAGMA user_version=2")
    before = path.read_bytes()
    result = _audit(path)
    assert result["expected_dates"] == 3 and len(result["missing"]) == 3
    assert result["projection_is_training_result"] is False
    assert result["observed_label_projection"] == [
        {
            "partition": "training",
            "expected_dates": 3,
            "diagnosed_dates": 2,
            "effective_t1_dates": 1,
            "minimum_effective_d25_dates": 1,
            "eligible_t1_rows": 5,
            "minimum_eligible_d25_rows": 3,
        }
    ]
    assert path.read_bytes() == before


def test_old_cache_audit_requires_rebuild_without_compatibility_read(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample_split_dates (trade_date INTEGER, split_name TEXT)")
        connection.run("CREATE TABLE samples (trade_date INTEGER, target_t1 REAL, target_d25_aggregate REAL)")
        connection.run("INSERT INTO sample_split_dates VALUES (20458, 'training')")
        connection.run("INSERT INTO samples VALUES (20458, 0.1, NULL)")
    with pytest.raises(ValueError, match="requires_rebuild"):
        _audit(path)
