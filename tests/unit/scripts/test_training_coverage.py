"""A cached coverage audit uses narrow statistics and reports conservative label bounds."""

from scripts.runtime_diagnostics.training_coverage import _audit
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def test_statistics_audit_does_not_read_wide_samples_and_separates_projection(tmp_path):
    path = tmp_path / "samples.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample_split_dates (trade_date TEXT, split_name TEXT)")
        connection.run("CREATE TABLE sample_statistics (trade_date TEXT, target TEXT, rows INTEGER)")
        connection.run(
            "CREATE TABLE sample_day_diagnostics (trade_date TEXT, candidates INTEGER, "
            "missing_t1 INTEGER, missing_t2 INTEGER, missing_t3 INTEGER, missing_t4 INTEGER, missing_t5 INTEGER)"
        )
        connection.run_many(
            "INSERT INTO sample_split_dates VALUES (?,?)",
            (
                ("2026-01-05", "training"),
                ("2026-01-06", "training"),
                ("2026-01-07", "training"),
            ),
        )
        connection.run("INSERT INTO sample_statistics VALUES ('2026-01-05', 'rows', 5)")
        connection.run("INSERT INTO sample_statistics VALUES ('2026-01-05', 'target_t1', 5)")
        connection.run("INSERT INTO sample_day_diagnostics VALUES ('2026-01-05', 5, 0, 1, 1, 0, 0)")
        connection.run("INSERT INTO sample_day_diagnostics VALUES ('2026-01-06', 5, 5, 5, 5, 5, 5)")
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


def test_legacy_cache_audit_reports_missing_targets_without_statistics(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample_split_dates (trade_date TEXT, split_name TEXT)")
        connection.run("CREATE TABLE samples (trade_date TEXT, target_t1 REAL, target_d25_aggregate REAL)")
        connection.run("INSERT INTO sample_split_dates VALUES ('2026-01-05', 'training')")
        connection.run("INSERT INTO samples VALUES ('2026-01-05', 0.1, NULL)")
    assert _audit(path) == {
        "expected_dates": 1,
        "missing": [{"date": "2026-01-05", "partition": "training", "sample_rows": 1, "t1_rows": 1, "d25_rows": 0}],
    }
