import hashlib
import time

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics.v5_lite import QualityPassed, _prepare
from trader.infra.atomic_files.json import AtomicWriteJson
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.training.infra.sample_sequences import SampleSequenceLayout
from trader.training.infra.sample_schema import SampleStorageId
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE


def test_lite_quality_limits_and_missing_values_fail_closed():
    baseline = {"root_mean_squared_error": 0.01, "mean_daily_rank_ic": 0.02}
    assert QualityPassed(baseline, {"root_mean_squared_error": 0.0104, "mean_daily_rank_ic": 0.011})
    assert not QualityPassed(baseline, {"root_mean_squared_error": 0.0106, "mean_daily_rank_ic": 0.02})
    assert not QualityPassed(baseline, {"root_mean_squared_error": 0.01, "mean_daily_rank_ic": 0.009})
    assert not QualityPassed(baseline, {"root_mean_squared_error": 0.01, "mean_daily_rank_ic": None})
    assert not QualityPassed(baseline, {"root_mean_squared_error": float("nan"), "mean_daily_rank_ic": 0.02})


def test_lite_diagnostic_requires_explicit_output_and_routes_sample_input(tmp_path):
    with pytest.raises(ValueError, match="training-probe-directory"):
        _validate(_parser().parse_args(["--profile", "v5-lite"]))
    options, _ = _validate(
        _parser().parse_args(
            [
                "--profile",
                "v5-lite",
                "--training-probe-directory",
                str(tmp_path / "new"),
                "--v5-sample-root",
                str(tmp_path / "source"),
            ]
        )
    )
    (command,) = build_commands(options)
    assert command.name == "v5_lite"
    assert command.argv[-2:] == ("--sample-root", str(tmp_path / "source"))


def test_real_comparison_snapshots_keep_source_and_evaluation_dates_unchanged(tmp_path):
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    database = source / "samples.sqlite3"
    options = SqliteConnectionOptions(timeout_seconds=1)
    with SqliteSession(database, options) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample_contract(singleton INTEGER PRIMARY KEY,split_hash BLOB)")
        connection.run("INSERT INTO sample_contract VALUES(1,?)", (b"a" * 32,))
        connection.run("CREATE TABLE sample_split_dates (split_name TEXT, trade_date INTEGER)")
        for index in range(750):
            connection.run("INSERT INTO sample_split_dates VALUES (?, ?)", ("training", index))
        connection.run("INSERT INTO sample_split_dates VALUES ('validation', 800)")
    original = database.read_bytes()
    AtomicWriteJson(
        source / "complete.json",
        {
            "storage": SampleStorageId(V5_TRAINING_PROFILE.heads[0].feature_manifest.names, SampleSequenceLayout(6, 60, 6)),
            "sha256": hashlib.sha256(original).hexdigest(),
        },
    )
    reference, candidate = _prepare(source, work, time.monotonic() + 10)
    assert database.read_bytes() == reference.read_bytes() == original
    with SqliteSession(candidate, options) as connection:
        assert tuple(
            connection.run(
                "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM sample_split_dates WHERE split_name='training'"
            ).fetchone()
        ) == (250, 500, 749)
        assert [
            tuple(row)
            for row in connection.run(
                "SELECT trade_date FROM sample_split_dates WHERE split_name='validation'"
            ).fetchall()
        ] == [(800,)]
    invalid = tmp_path / "invalid"
    invalid.mkdir()
    AtomicWriteJson(
        source / "complete.json",
        {
            "storage": SampleStorageId(V5_TRAINING_PROFILE.heads[0].feature_manifest.names, SampleSequenceLayout(6, 60, 6)),
            "sha256": "0" * 64,
        },
    )
    with pytest.raises(ValueError, match="digest"):
        _prepare(source, invalid, time.monotonic() + 10)
