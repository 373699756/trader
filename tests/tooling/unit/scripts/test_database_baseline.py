from __future__ import annotations

from tests.helpers.shared.tool_cli import BuildToolCommands, ParseBusinessOptions, ParseToolArguments

from scripts.core.catalog import SelectionArguments

from tests.helpers.shared.configuration import SqlitePolicyFixture

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.core.execution import ExecuteCommand
from scripts.infra.database import database_workspace
from scripts.infra.database.database_baseline import MeasureSyncBaseline
from scripts.infra.database.database_baseline import _compare
from scripts.infra.database.database_measurement import DatabaseMeasurement
from scripts.infra.database.database_workspace import DatabaseWorkerEnvironment
from scripts.infra.database.database_workspace import PrepareDatabaseProbeDirectory
from scripts.infra.database.database_workspace import ProjectDatabaseProbeLocation
from scripts.core.process import RunBoundedCommand
from scripts.core.contracts import CommandPreparation
from scripts.core.paths import PROJECT_ROOT
from scripts.core.contracts import DiagnosticCommand
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction
from trader.infra.disk_types import DiskProfile
from scripts.infra.database import database_baseline

_CONFIG = PROJECT_ROOT / "config/runtime.json"


def test_training_io_baseline_measures_all_profiles_and_restores_instrumentation(tmp_path, monkeypatch):
    initialize, close, run = SqliteConnection.__init__, SqliteConnection.close, SqliteConnection.run
    monkeypatch.setattr(database_baseline, "MeasureSyncBaseline", lambda *args, **kwargs: {})
    monkeypatch.setattr(database_baseline, "MeasureHistoryWrites", lambda *args, **kwargs: {"status": "passed", "summary": {}})
    report = database_baseline._worker(2, 64, "hdd", sqlite_policy=SqlitePolicyFixture(), include_training=True)
    training = report["training"]
    assert set(training["samples"]["cases"]) == {"v1", "v2", "v3", "v5"}
    assert all(len(case["numeric_hash"]) == 64 for case in training["samples"]["cases"].values())
    assert training["measurement"]["connections"] > 0
    assert training["measurement"]["statements"] > 0
    assert training["samples"]["database"]["connections"] == training["samples"]["database"]["closed_connections"]
    assert (SqliteConnection.__init__, SqliteConnection.close, SqliteConnection.run) == (initialize, close, run)


def test_training_baseline_is_routed_only_to_a_new_isolated_baseline(tmp_path):
    base = [*SelectionArguments('database-baseline'), "--database-probe-directory", str(tmp_path / "probe")]
    options = ParseBusinessOptions(ParseToolArguments(base + ["--database-training"]))
    assert "--include-training" in BuildToolCommands(options, python_executable="/python3")[0].argv
    with pytest.raises(ValueError, match="without a comparison checkout"):
        ParseBusinessOptions(ParseToolArguments(base + ["--database-training", "--baseline-checkout", str(tmp_path / "baseline")]))
    with pytest.raises(SystemExit) as error:
        ParseToolArguments([*SelectionArguments('snapshot'), "--disk-policy-audit"])
    assert error.value.code == 2


@pytest.mark.parametrize("training", (False, True))
@pytest.mark.parametrize("changed", (False, True))
def test_baseline_checks_cross_round_facts_in_every_mode(training, changed, monkeypatch, capsys):
    number = 0

    def worker(*args, **kwargs):
        nonlocal number
        number += 1
        return {"history": {"rows": number if changed else 3, "wall_seconds": float(number)}}

    monkeypatch.setattr(database_baseline, "_run_worker", worker)
    monkeypatch.setattr(database_baseline, "_input_identity", lambda: "fixed")
    monkeypatch.setattr(
        sys,
        "argv",
        ["database-baseline", "--rounds", "2", *(("--include-training",) if training else ())],
    )
    if changed:
        with pytest.raises(RuntimeError, match="repeated_input_or_output_mismatch"):
            database_baseline.Main()
        assert not capsys.readouterr().out
    else:
        assert database_baseline.Main() == 0
        assert json.loads(capsys.readouterr().out)["status"] == "passed"


@pytest.mark.parametrize(
    "relative",
    ("src/trader/example.py", "scripts/infra/database/example.py", "config/runtime.json"),
)
def test_baseline_identity_tracks_each_selected_checkout_input(tmp_path, relative):
    source = tmp_path / relative
    source.parent.mkdir(parents=True)
    source.write_text("original", encoding="utf-8")
    before = database_baseline._input_identity(tmp_path)
    assert database_baseline._input_identity(tmp_path) == before
    source.write_text("changed", encoding="utf-8")
    assert database_baseline._input_identity(tmp_path) != before


def test_measurement_counts_wrapper_operations_and_preserves_rollback(tmp_path: Path) -> None:
    original = SqliteConnection.run
    path = tmp_path / "facts.sqlite3"
    with DatabaseMeasurement(tmp_path) as measured, SqliteSession(path) as connection:
        connection.run("CREATE TABLE facts(value INTEGER)")
        with SqliteTransaction(connection):
            connection.run_many("INSERT INTO facts VALUES(?)", ((value,) for value in range(3)))
        assert [row[0] for row in connection.run("SELECT value FROM facts ORDER BY value")] == [0, 1, 2]
        with pytest.raises(ValueError, match="cancel"), SqliteTransaction(connection):
            connection.run("DELETE FROM facts")
            raise ValueError("cancel")
    assert SqliteConnection.run is original
    assert measured.result["connections"] == 1
    assert measured.result["statements"] == 8
    assert measured.result["transactions"] == 2
    assert measured.result["commits"] == 1
    assert measured.result["sqlite_call_seconds"] >= measured.result["commit_seconds"] >= 0
    with SqliteSession(path) as connection:
        assert connection.run("SELECT COUNT(*) FROM facts").require_one()[0] == 3
    with pytest.raises(ValueError), DatabaseMeasurement(tmp_path) as failed:
        raise ValueError("failure")
    assert failed.result["completed"] is False
    assert SqliteConnection.run is original


@pytest.mark.parametrize(
    "filesystem, medium, expected, source",
    (
        ("tmpfs", "ssd", "unknown", "memory_filesystem"),
        ("ext4", "ssd", "unknown", "automatic"),
        ("ext4", None, "unknown", "automatic"),
    ),
)
def test_workspace_ignores_app_medium_and_rejects_memory_disk_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filesystem, medium, expected, source
) -> None:
    settings = replace(LoadRuntimeSettings(_CONFIG), disk_medium=medium)
    monkeypatch.setattr(database_workspace, "LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(database_workspace, "_filesystem", lambda _: filesystem)
    monkeypatch.setattr(
        database_workspace.DiskProbe,
        "inspect",
        lambda *_: DiskProfile("unknown", "virtual", "automatic", "device_metadata_unavailable"),
    )
    location = PrepareDatabaseProbeDirectory(tmp_path / "probe", _CONFIG)
    assert location.disk_kind == expected
    assert location.disk_source == source
    evidence = ProjectDatabaseProbeLocation(location)
    assert evidence["memory_backed"] is (filesystem == "tmpfs")
    assert evidence["disk_performance_evidence"] is False


def test_workspace_rejects_source_existing_relative_and_symlink_targets(tmp_path: Path) -> None:
    for target in (PROJECT_ROOT / "probe", tmp_path, Path("relative-probe"), PROJECT_ROOT.parent):
        with pytest.raises(ValueError):
            PrepareDatabaseProbeDirectory(target, _CONFIG)
    source_root = LoadRuntimeSettings(_CONFIG).data_paths.root
    alias = tmp_path / "active-link"
    alias.symlink_to(source_root, target_is_directory=True)
    with pytest.raises(ValueError):
        PrepareDatabaseProbeDirectory(alias / "probe", _CONFIG)
    assert not (PROJECT_ROOT / "probe").exists()


def test_database_profile_requires_external_directory_and_bounds_workload(tmp_path: Path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="requires --database-probe-directory"):
        ParseBusinessOptions(ParseToolArguments([*SelectionArguments('database-baseline')]))
    base = [*SelectionArguments('database-baseline'), "--database-probe-directory", str(tmp_path / "new")]
    options = ParseBusinessOptions(ParseToolArguments(base))
    calls = []
    def prepare(directory, configuration, command_name):
        calls.append((directory, configuration, command_name))
        return CommandPreparation()
    monkeypatch.setattr("scripts.infra.database.commands.PrepareDatabaseCommand", prepare)
    command = BuildToolCommands(options, python_executable=sys.executable)[0]
    assert command.prepare is not None
    assert calls == []
    assert command.prepare() == CommandPreparation()
    assert calls == [(tmp_path / "new", options.runtime_config, "database_baseline")]
    assert command.argv[-6:] == ("--size", "8", "--sessions", "61", "--rounds", "5")
    for bad in (("--download-sizes", "33"), ("--history-days", "2001"), ("--download-sizes", "1", "2")):
        with pytest.raises(ValueError):
            ParseBusinessOptions(ParseToolArguments(base + list(bad)))


@pytest.mark.parametrize("command_name", ("fixture", "history_write_benchmark", "database_baseline"))
def test_worker_tempfiles_and_filesystem_evidence_reach_the_report(tmp_path: Path, command_name: str) -> None:
    target = tmp_path / "isolated"
    script = (
        "import argparse,json,tempfile; from pathlib import Path; "
        "parser=argparse.ArgumentParser(); parser.add_argument('--disk-kind',choices=('hdd','ssd','unknown')); "
        "args=parser.parse_args(); "
        "print(json.dumps({'status':'passed','summary':{'temporary_root':str(Path(tempfile.gettempdir())),"
        "'disk_kind':args.disk_kind}}))"
    )
    target.mkdir()
    preparation = CommandPreparation(
        ("--disk-kind", "unknown") if command_name != "fixture" else (),
        DatabaseWorkerEnvironment(target),
        {"database_workspace": {"filesystem": "fixture", "disk_kind": "unknown"}},
    )
    result = ExecuteCommand(DiagnosticCommand(
        command_name, (sys.executable, "-c", script), 10, prepare=lambda: preparation,
    ))
    assert result.error_code is None
    assert result.payload["summary"]["temporary_root"] == str(target)
    assert "database_workspace" in result.payload["summary"]
    assert result.payload["summary"]["database_workspace"]["filesystem"] != "unknown"
    if command_name != "fixture":
        assert result.payload["summary"]["disk_kind"] == result.payload["summary"]["database_workspace"]["disk_kind"]


def test_database_comparison_routes_the_external_baseline_to_the_same_harness(tmp_path: Path) -> None:
    options = ParseBusinessOptions(
        ParseToolArguments(
            [
                *SelectionArguments('database-baseline'),
                "--database-probe-directory",
                str(tmp_path / "probe"),
                "--baseline-checkout",
                str(tmp_path / "baseline"),
                "--database-rounds",
                "5",
                "--database-acceptance-focus",
                "market-company",
            ]
        )
    )
    command = BuildToolCommands(options, python_executable=sys.executable)[0]
    assert command.argv[-2:] == ("--baseline-checkout", str(tmp_path / "baseline"))
    assert command.argv[command.argv.index("--rounds") + 1] == "5"
    assert command.argv[command.argv.index("--acceptance-focus") + 1] == "market-company"
    assert command.name == "database_baseline"


@pytest.mark.parametrize("focus", ("history", "market-company"))
@pytest.mark.parametrize(
    "failure",
    (
        None,
        "rss",
        "identity",
        "company_no_gain",
        "company_space",
        "company_startup",
        "identity_drift",
        "baseline_source",
        "candidate_source",
    ),
)
def test_comparison_requires_equal_publications_and_every_round_within_memory_budget(
    tmp_path, monkeypatch, failure, focus
):
    baseline = tmp_path / "baseline"
    (baseline / "src/trader").mkdir(parents=True)
    candidate_round = 0
    calls = []

    def input_identity(checkout):
        variant = "candidate" if checkout == PROJECT_ROOT else "baseline"
        return "changed" if failure == f"{variant}_source" and variant in calls else "fixed"

    monkeypatch.setattr(database_baseline, "_input_identity", input_identity)

    def worker(checkout, size, sessions, disk_kind, *, runtime_config):
        nonlocal candidate_round
        assert runtime_config == _CONFIG
        candidate = checkout == PROJECT_ROOT
        candidate_round += int(candidate)
        calls.append("candidate" if candidate else "baseline")
        seconds = 0.8 if candidate else 1.0
        rss = 2 * 1024**3 + 1 if candidate and candidate_round == 1 and failure == "rss" else 1024
        identity = "changed" if candidate and failure == "identity" else "same"
        if failure == "identity_drift":
            identity = f"pair-{(len(calls) - 1) // 2}"
        return {
            "history": {
                "cases": [
                    {
                        "case": name,
                        "publication_hash": identity,
                        "files_bytes": 1024,
                        "database": {"wall_seconds": seconds, "process_peak_rss_bytes": rss},
                    }
                    for name in ("initial", "next_day", "two_weeks", "same_day", "initial_resume")
                ]
            },
            "sync": {
                "targets": {
                    target: {
                        "initial": {
                            "wall_seconds": 1.0 if target == "company" and failure == "company_no_gain" else seconds,
                            "files_bytes": 2048
                            if candidate and target == "company" and failure == "company_space"
                            else 1024,
                        },
                        "same_day": {
                            "wall_seconds": 1.0 if target == "company" and failure == "company_no_gain" else seconds
                        },
                        "startup": {
                            "identities": {"history": identity},
                            "phases": {
                                "startup_history": {"wall_seconds": seconds},
                                "startup_company": {
                                    "wall_seconds": 1.1
                                    if candidate and target == "company" and failure == "company_startup"
                                    else seconds
                                },
                            },
                        },
                    }
                    for target in ("all", "history", "market", "company")
                }
            },
        }

    monkeypatch.setattr("scripts.infra.database.database_baseline._run_worker", worker)
    if failure == "identity":
        with pytest.raises(RuntimeError, match="publication_changed"):
            _compare(baseline, 2, 61, 5, "unknown", focus)
    elif failure == "identity_drift":
        with pytest.raises(RuntimeError, match="repeated_input_or_output_mismatch"):
            _compare(baseline, 2, 61, 5, "unknown", focus)
    elif failure in {"baseline_source", "candidate_source"}:
        with pytest.raises(RuntimeError, match="code_or_configuration_changed"):
            _compare(baseline, 2, 61, 5, "unknown", focus)
    else:
        report = _compare(baseline, 2, 61, 5, "unknown", focus)
        assert report["acceptance"]["accepted"] is (
            failure not in {"rss", "company_startup"}
            and not (focus == "market-company" and failure == "company_no_gain")
        )
        assert report["acceptance"]["database_size_within_5_percent"] is (failure != "company_space")
        assert report["acceptance"]["process_rss_within_2_gib"] is (failure != "rss")
        assert report["input_identities"] == {"baseline": "fixed", "candidate": "fixed"}
        assert calls == [
            "baseline",
            "candidate",
            "candidate",
            "baseline",
            "baseline",
            "candidate",
            "candidate",
            "baseline",
            "baseline",
            "candidate",
        ]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group diagnostic")
def test_timeout_terminates_nested_probe_workers(tmp_path: Path) -> None:
    child_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        RunBoundedCommand(
            (sys.executable, "-c", script, str(child_file)), timeout=1, environment=DatabaseWorkerEnvironment(tmp_path)
        )
    pid = int(child_file.read_text())
    # SIGKILL delivery and reaping of the grandchild can lag the parent's wait().
    stat = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 2
    while True:
        try:
            state = stat.read_text().split(") ", 1)[1].split()[0]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        assert time.monotonic() < deadline, f"probe grandchild still active: {state}"
        time.sleep(0.01)


def test_real_sync_adapters_preserve_joint_single_restart_and_memory_reads(tmp_path: Path) -> None:
    result = MeasureSyncBaseline(tmp_path, size=2, sessions=61, disk_kind="unknown", sqlite_policy=SqlitePolicyFixture())
    assert result["joint_single_equivalent"] is True
    assert result["independent_targets"] is True
    targets = result["targets"]
    for target in ("all", "history", "market", "company"):
        data = targets[target]
        assert data["initial"]["files_bytes"] > 0
        assert data["same_day"]["files_bytes"] > 0
        assert data["initial"]["prepare_calls"] == data["same_day"]["prepare_calls"] == 1
        assert data["startup"]["identities"] == data["restarted"]["identities"]
        assert not any(name.startswith("startup") for name in data["same_day"]["phases"])
        assert data["startup"]["phases"]["loaded_memory_reads"]["connections"] == 0
        assert data["startup"]["history_available"] is (target in {"all", "history"})
        if target in {"all", "company"}:
            company = data["initial"]["phases"]["company"]
            assert company["connections"] > 0 and company["commits"] > 0
    assert targets["all"]["startup"]["history_scan_count"] == 1
    assert targets["all"]["startup"]["profile_entries"] == [2, 2, 2, 2]
