from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.diagnose_runtime import _parser, _validate, build_commands, execute_command
from scripts.runtime_diagnostics import database_workspace
from scripts.runtime_diagnostics.database_baseline import MeasureSyncBaseline, _compare
from scripts.runtime_diagnostics.database_measurement import DatabaseMeasurement
from scripts.runtime_diagnostics.database_workspace import (
    DatabaseWorkerEnvironment,
    PrepareDatabaseProbeDirectory,
    ProjectDatabaseProbeLocation,
    RunDatabaseProbe,
)
from scripts.runtime_diagnostics.diagnostic_types import PROJECT_ROOT, DiagnosticCommand
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.sqlite_connection import SqliteConnection, SqliteSession, SqliteTransaction
from trader.infra.storage_types import StorageOverride, StorageProfile

_CONFIG = PROJECT_ROOT / "config/runtime.json"


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
    "filesystem, kinds, expected, source",
    (
        ("tmpfs", ("ssd",), "unknown", "memory_filesystem"),
        ("ext4", ("ssd",), "ssd", "configuration_same_filesystem"),
        ("ext4", ("ssd", "hdd"), "unknown", "automatic"),
    ),
)
def test_workspace_distinguishes_memory_and_manual_same_filesystem_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filesystem, kinds, expected, source
) -> None:
    settings = replace(
        LoadRuntimeSettings(_CONFIG),
        storage_overrides=tuple(
            StorageOverride(tmp_path / f"configured-{offset}", kind) for offset, kind in enumerate(kinds)
        ),
    )
    monkeypatch.setattr(database_workspace, "LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr(database_workspace, "_filesystem", lambda _: filesystem)
    monkeypatch.setattr(
        database_workspace.StorageProbe,
        "inspect",
        lambda *_: StorageProfile("unknown", "virtual", "automatic", "device_metadata_unavailable"),
    )
    location = PrepareDatabaseProbeDirectory(tmp_path / "probe", _CONFIG)
    assert location.storage_kind == expected
    assert location.storage_source == source
    evidence = ProjectDatabaseProbeLocation(location)
    assert evidence["memory_backed"] is (filesystem == "tmpfs")
    assert evidence["disk_performance_evidence"] is (filesystem == "ext4" and expected == "ssd")


def test_workspace_rejects_active_existing_relative_and_symlink_targets(tmp_path: Path) -> None:
    for target in (PROJECT_ROOT / "probe", tmp_path, Path("relative-probe"), PROJECT_ROOT.parent):
        with pytest.raises(ValueError):
            PrepareDatabaseProbeDirectory(target, _CONFIG)
    active = LoadRuntimeSettings(_CONFIG).data_paths.root
    alias = tmp_path / "active-link"
    alias.symlink_to(active, target_is_directory=True)
    with pytest.raises(ValueError):
        PrepareDatabaseProbeDirectory(alias / "probe", _CONFIG)
    assert not (PROJECT_ROOT / "probe").exists()


def test_database_profile_requires_external_directory_and_bounds_workload(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires --database-probe-directory"):
        _validate(_parser().parse_args(["--profile", "database-baseline"]))
    base = ["--profile", "database-baseline", "--database-probe-directory", str(tmp_path / "new")]
    options, _ = _validate(_parser().parse_args(base))
    command = build_commands(options, python_executable=sys.executable)[0]
    assert command.database_probe_directory == tmp_path / "new"
    assert command.argv[-6:] == ("--size", "8", "--sessions", "61", "--rounds", "5")
    for bad in (("--download-sizes", "33"), ("--history-days", "2001"), ("--download-sizes", "1", "2")):
        with pytest.raises(ValueError):
            _validate(_parser().parse_args(base + list(bad)))


def test_worker_tempfiles_and_filesystem_evidence_reach_the_report(tmp_path: Path) -> None:
    target = tmp_path / "isolated"
    script = (
        "import json,tempfile; from pathlib import Path; "
        "print(json.dumps({'status':'passed','summary':{'temporary_root':str(Path(tempfile.gettempdir()))}}))"
    )
    result = execute_command(DiagnosticCommand("fixture", (sys.executable, "-c", script), 10, target, _CONFIG))
    assert result.error_code is None
    assert result.payload["summary"]["temporary_root"] == str(target)
    assert "database_workspace" in result.payload["summary"]
    assert result.payload["summary"]["database_workspace"]["filesystem"] != "unknown"


def test_database_comparison_routes_the_external_baseline_to_the_same_harness(tmp_path: Path) -> None:
    options, _ = _validate(
        _parser().parse_args(
            [
                "--profile",
                "database-baseline",
                "--database-probe-directory",
                str(tmp_path / "probe"),
                "--baseline-checkout",
                str(tmp_path / "baseline"),
                "--database-rounds",
                "5",
            ]
        )
    )
    command = build_commands(options, python_executable=sys.executable)[0]
    assert command.argv[-2:] == ("--baseline-checkout", str(tmp_path / "baseline"))
    assert command.argv[command.argv.index("--rounds") + 1] == "5"
    assert command.name == "database_baseline"


@pytest.mark.parametrize("failure", (None, "rss", "identity"))
def test_comparison_requires_equal_publications_and_every_round_within_memory_budget(tmp_path, monkeypatch, failure):
    baseline = tmp_path / "baseline"
    (baseline / "src/trader").mkdir(parents=True)
    candidate_round = 0
    calls = []

    def worker(checkout, size, sessions, storage_kind):
        nonlocal candidate_round
        candidate = checkout == PROJECT_ROOT
        candidate_round += int(candidate)
        calls.append("candidate" if candidate else "baseline")
        seconds = 0.8 if candidate else 1.0
        rss = 2 * 1024**3 + 1 if candidate and candidate_round == 1 and failure == "rss" else 1024
        identity = "changed" if candidate and failure == "identity" else "same"
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
                        "initial": {"wall_seconds": seconds},
                        "same_day": {"wall_seconds": seconds},
                        "startup": {
                            "identities": {"history": identity},
                            "phases": {"startup_history": {"wall_seconds": seconds}},
                        },
                    }
                    for target in ("all", "history", "market", "company")
                }
            },
        }

    monkeypatch.setattr("scripts.runtime_diagnostics.database_baseline._run_worker", worker)
    if failure == "identity":
        with pytest.raises(RuntimeError, match="publication_changed"):
            _compare(baseline, 2, 61, 5, "unknown")
    else:
        report = _compare(baseline, 2, 61, 5, "unknown")
        assert report["acceptance"]["accepted"] is (failure is None)
        assert report["acceptance"]["process_rss_within_2_gib"] is (failure is None)
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
        RunDatabaseProbe(
            (sys.executable, "-c", script, str(child_file)), timeout=1, environment=DatabaseWorkerEnvironment(tmp_path)
        )
    pid = int(child_file.read_text())
    # A killed grandchild may await reaping by init, but cannot keep writing.
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        assert stat.read_text().split(") ", 1)[1].split()[0] == "Z"


def test_real_sync_adapters_preserve_joint_single_restart_and_memory_reads(tmp_path: Path) -> None:
    result = MeasureSyncBaseline(tmp_path, size=2, sessions=61, storage_kind="unknown")
    assert result["joint_single_equivalent"] is True
    assert result["independent_targets"] is True
    targets = result["targets"]
    for target in ("all", "history", "market", "company"):
        data = targets[target]
        assert data["initial"]["prepare_calls"] == data["same_day"]["prepare_calls"] == 1
        assert data["startup"]["identities"] == data["restarted"]["identities"]
        assert not any(name.startswith("startup") for name in data["same_day"]["phases"])
        assert data["startup"]["phases"]["loaded_memory_reads"]["connections"] == 0
        assert data["startup"]["history_available"] is (target in {"all", "history"})
    assert targets["all"]["startup"]["history_scan_count"] == 1
    assert targets["all"]["startup"]["profile_entries"] == [2, 2, 2, 2]
