
from tests.helpers.configuration import SqlitePolicyFixture
import hashlib
import json
import time

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands
from scripts.runtime_diagnostics.v5_lite import QualityPassed, _prepare
from trader.infra.atomic_files.json import AtomicWriteJson
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession, SqliteTransaction
from trader.training.infra.sample_sequences import SampleSequenceLayout
from trader.training.infra.sample_schema import SampleStorageId
from trader.training.infra.profile.v5.contracts import V5_TRAINING_PROFILE
from tests.helpers.configuration import TcnFittingParametersFixture


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
                "--v5-candidate-config",
                str(tmp_path / "candidate.json"),
                "--v5-sample-root",
                str(tmp_path / "source"),
            ]
        )
    )
    (command,) = build_commands(options)
    assert command.name == "v5_lite"
    assert command.argv[-2:] == ("--sample-root", str(tmp_path / "source"))
    assert "--candidate-config" in command.argv
    with pytest.raises(ValueError, match="v5-candidate-config"):
        _validate(_parser().parse_args([
            "--profile", "v5-lite", "--training-probe-directory", str(tmp_path / "new"),
            "--v5-sample-root", str(tmp_path / "source"),
        ]))


@pytest.mark.parametrize("source_days", (61, 250, 750))
def test_real_comparison_snapshots_keep_source_and_evaluation_dates_unchanged(tmp_path, source_days):
    source, work = tmp_path / "source", tmp_path / "work"
    source.mkdir()
    work.mkdir()
    database = source / "samples.sqlite3"
    options = SqliteConnectionOptions(timeout_seconds=1)
    with SqliteSession(database, options) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE sample_contract(singleton INTEGER PRIMARY KEY,split_hash BLOB)")
        connection.run("INSERT INTO sample_contract VALUES(1,?)", (b"a" * 32,))
        connection.run("CREATE TABLE sample_split_dates (split_name TEXT, trade_date INTEGER)")
        for index in range(source_days):
            connection.run("INSERT INTO sample_split_dates VALUES (?, ?)", ("training", index))
        connection.run("INSERT INTO sample_split_dates VALUES ('validation', 800)")
        connection.run("INSERT INTO sample_split_dates VALUES ('early_stopping', 801)")
        connection.run("INSERT INTO sample_split_dates VALUES ('calibration', 802)")
        connection.run("INSERT INTO sample_split_dates VALUES ('holdout', 803)")
    original = database.read_bytes()
    AtomicWriteJson(
        source / "complete.json",
        {
            "storage": SampleStorageId(V5_TRAINING_PROFILE.heads[0].feature_manifest.names, SampleSequenceLayout(6, 60, 6)),
            "sha256": hashlib.sha256(original).hexdigest(),
        },
    )
    reference, candidate = _prepare(source, work, time.monotonic() + 10, 61, sqlite_policy=SqlitePolicyFixture())
    assert database.read_bytes() == original
    assert reference.read_bytes() == candidate.read_bytes()
    with SqliteSession(candidate, options) as connection:
        assert tuple(
            connection.run(
                "SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM sample_split_dates WHERE split_name='training'"
            ).fetchone()
        ) == (61, source_days - 61, source_days - 1)
        assert [
            tuple(row)
            for row in connection.run(
                "SELECT split_name, trade_date FROM sample_split_dates WHERE split_name!='training' ORDER BY trade_date"
            ).fetchall()
        ] == [("validation", 800), ("early_stopping", 801), ("calibration", 802), ("holdout", 803)]
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
        _prepare(source, invalid, time.monotonic() + 10, 61, sqlite_policy=SqlitePolicyFixture())


def test_parameter_comparison_rejects_changed_sample_scope_before_copy(tmp_path):
    from dataclasses import replace

    from scripts.runtime_diagnostics.v5_lite import _quality
    parameters = TcnFittingParametersFixture(minimum_industry_training_rows=5000)
    for candidate in ((250, parameters), (61, replace(parameters, minimum_industry_training_rows=10000))):
        with pytest.raises(ValueError, match="identical fit dates"):
            _quality(tmp_path / "absent", tmp_path, time.monotonic() + 10, (61, parameters), candidate, sqlite_policy=SqlitePolicyFixture())
    assert not tuple(tmp_path.iterdir())


@pytest.mark.parametrize("interrupted", (False, True))
def test_compute_only_or_interrupted_probe_cannot_claim_quality_passed(tmp_path, monkeypatch, capsys, interrupted):
    from scripts.runtime_diagnostics import v5_lite

    work = tmp_path / "probe"
    monkeypatch.setattr("sys.argv", ["v5_lite", "--work-root", str(work)])

    def compute():
        if interrupted:
            raise KeyboardInterrupt
        return {"median_seconds_per_batch": {"fixture": 0.01}}

    monkeypatch.setattr(v5_lite, "ProbeLiteKernels", compute)
    assert v5_lite.Main() == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "degraded"
    summary = report["summary"]
    assert summary["full_training"] is False and summary["production_authority"] is False
    if interrupted:
        assert summary["failure"] == "interrupted"
    else:
        assert summary["reason"] == "quality_not_requested"
    assert not tuple(work.glob("*.sqlite3"))
