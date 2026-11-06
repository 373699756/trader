"""Maintenance dispatch uses explicit actions and preserves native completion states."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts.core.paths import PROJECT_ROOT
from scripts.download.history import repack
from scripts.engineering.migration import cli as migration_cli
from scripts.recommendation.assets import cli as assets_cli


@pytest.mark.parametrize(
    "module,identity,arguments",
    (
        (migration_cli, "migration", ["--sou", "/external/source", "--config", "/external/config.json"]),
        (migration_cli, "migration-build", ["--source", "/external/source", "--tar", "/external/target", "--default-profile", "v2"]),
        (migration_cli, "migration-rollback", ["--tar", "/external/target", "--backup", "/external/backup"]),
        (repack, "history-repack-build", ["--source", "/external/source", "--tar", "/external/target"]),
        (repack, "history-repack-activate", ["--source", "/external/source", "--tar", "/external/target"]),
        (repack, "history-repack-rollback", ["--source", "/external/source", "--tar", "/external/target"]),
        (repack, "history-repack-finalize", ["--source", "/external/source", "--tar", "/external/target", "--training-root", "/external/training", "--memory-evidence", "/external/memory.json"]),
    ),
)
def test_maintenance_rejects_abbreviated_paths_before_loading_configuration_or_running(module, identity, arguments, monkeypatch) -> None:
    policy = Mock(side_effect=AssertionError("configuration loaded"))
    coordinator = Mock(side_effect=AssertionError("maintenance started"))
    monkeypatch.setattr(migration_cli, "_sqlite_policy", policy)
    monkeypatch.setattr(repack, "_coordinator", coordinator)
    with pytest.raises(SystemExit) as failure:
        module.Run(identity, arguments)
    assert failure.value.code == 2
    policy.assert_not_called()
    coordinator.assert_not_called()


@pytest.mark.parametrize(
    "identity,arguments",
    (
        ("memory", ["-h"]),
        ("memory", ["--profile", "v3", "--help"]),
        ("wheel-install", ["-h"]),
        ("quality-debt", ["-h"]),
        ("quality-transform", ["--help"]),
        ("skill-evaluation", ["build-case", "--name", "example", "-h"]),
    ),
)
def test_native_help_uses_tool_output_without_json_parsing_or_business_dispatch(identity, arguments, monkeypatch, capsys) -> None:
    from scripts.core.catalog import SelectionArguments
    from scripts.core.cli import Main
    from scripts.core.contracts import DiagnosticResult
    from scripts.core import execution

    execute = Mock(return_value=DiagnosticResult(identity, 0, 0.0, {"summary": {"tool_output": "usage: fixture\n"}}, None))
    dispatch = Mock(side_effect=AssertionError("business dispatch started"))
    monkeypatch.setattr(execution, "ExecuteCommand", execute)
    monkeypatch.setattr(execution, "RunDiagnostics", dispatch)
    assert Main([*SelectionArguments(identity), *arguments]) == 0
    assert capsys.readouterr().out == "usage: fixture\n"
    command = execute.call_args.args[0]
    assert command.output_kind == "tool" and command.timeout_seconds == 15
    assert command.argv[-len(arguments):] == tuple(arguments)
    dispatch.assert_not_called()


@pytest.mark.parametrize("interruption", ("timeout", "cancel"))
def test_outer_interruption_reaps_a_nested_session_through_worker_finally(tmp_path, monkeypatch, interruption) -> None:
    import os
    import signal
    import time

    from scripts.core import process as process_tools

    if os.name != "posix":
        pytest.skip("owned process groups require POSIX")
    child_pid = tmp_path / "child.pid"
    cleaned = tmp_path / "cleaned"
    child = (
        "import os,sys,time\nfrom pathlib import Path\n"
        "Path(sys.argv[1]).write_text(str(os.getpid()))\ntime.sleep(60)\n"
    )
    worker = (
        "import sys\nfrom pathlib import Path\n"
        "from scripts.recommendation.web.company_ledger_browser import _run_browser\n"
        "try:\n _run_browser([sys.executable, '-c', sys.argv[1], sys.argv[2]])\n"
        "finally:\n Path(sys.argv[3]).write_text('cleaned')\n"
    )
    original_sleep = time.sleep
    cancelled = False

    def cancel_when_child_ready(seconds: float) -> None:
        nonlocal cancelled
        if not cancelled and child_pid.exists() and child_pid.read_text().strip():
            cancelled = True
            raise KeyboardInterrupt
        original_sleep(seconds)

    try:
        with monkeypatch.context() as controls:
            if interruption == "cancel":
                controls.setattr(process_tools.time, "sleep", cancel_when_child_ready)
            with pytest.raises(subprocess.TimeoutExpired if interruption == "timeout" else KeyboardInterrupt):
                process_tools.RunBoundedCommand(
                    (sys.executable, "-c", worker, child, str(child_pid), str(cleaned)),
                    timeout=10,
                    environment=dict(os.environ),
                )
        assert cleaned.read_text() == "cleaned"
        pid = int(child_pid.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if child_pid.exists() and child_pid.read_text().strip():
            try:
                os.killpg(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "module,identity",
    (
        ("scripts.recommendation.assets.cli", "assets"),
        ("scripts.recommendation.assets.cli", "assets-generate"),
        ("scripts.engineering.migration.cli", "migration"),
        ("scripts.engineering.migration.cli", "migration-build"),
        ("scripts.engineering.migration.cli", "migration-rollback"),
        ("scripts.download.history.repack", "history-repack-build"),
        ("scripts.download.history.repack", "history-repack-activate"),
        ("scripts.download.history.repack", "history-repack-rollback"),
        ("scripts.download.history.repack", "history-repack-finalize"),
    ),
)
def test_help_finishes_before_loading_maintenance_or_configuration(module: str, identity: str) -> None:
    code = (
        "import importlib,sys\n"
        "module=importlib.import_module(sys.argv[1])\n"
        "try:\n module.Run(sys.argv[2], ['--help'])\n"
        "except SystemExit as error:\n assert error.code == 0\n"
        "else:\n raise AssertionError('help did not exit')\n"
        "assert not any(name in sys.modules for name in sys.argv[3:])\n"
    )
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            code,
            module,
            identity,
            "scripts.recommendation.assets.generation",
            "scripts.engineering.migration.operations",
            "trader.bootstrap",
            "trader.infra.settings",
            "trader.download.infra.history_partition_repack",
            "trader.training.infra.history.repack_training_evidence",
        ),
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout


def test_asset_consistency_cannot_write_and_generation_reuses_the_same_rendering(monkeypatch) -> None:
    from scripts.recommendation.assets import generation

    source = Mock(spec=Path)
    source.read_text.return_value = '{"codes":["600000"],"label":"观察"}'
    asset = Mock(spec=Path)
    asset.is_file.return_value = True
    expected = '(function(){"use strict";window.TraderLongWatchlistData=Object.freeze({"codes":["600000"],"label":"观察"});})();\n'
    asset.read_text.return_value = expected
    monkeypatch.setattr(generation, "CONFIG_PATH", source)
    monkeypatch.setattr(generation, "ASSET_PATH", asset)

    checked = assets_cli.Run("assets", [])
    assert checked.exit_code == 0
    assert checked.payload["summary"] == {"asset_matches_source": True, "read_only": True}
    asset.write_text.assert_not_called()

    generated = assets_cli.Run("assets-generate", [])
    assert generated.exit_code == 0
    assert generated.payload["state"] == "generated"
    asset.write_text.assert_called_once_with(expected, encoding="utf-8")
    assert source.read_text.call_count == 2


def test_asset_mismatch_is_a_failed_read_only_result(monkeypatch) -> None:
    from scripts.recommendation.assets import generation

    asset = Mock(spec=Path)
    asset.is_file.return_value = False
    monkeypatch.setattr(generation, "ASSET_PATH", asset)
    monkeypatch.setattr(generation, "RenderAsset", lambda: "expected")
    outcome = assets_cli.Run("assets", [])
    assert outcome.exit_code == 1
    assert outcome.payload["status"] == "failed"
    asset.write_text.assert_not_called()


def test_migration_build_keeps_candidate_schema_and_failure_code(monkeypatch, tmp_path: Path) -> None:
    from scripts.engineering.migration import operations

    manifest = operations.MigrationManifest("a" * 64, "b" * 64, 0, 0, ("models",), ())
    candidate = Mock(return_value=manifest)
    monkeypatch.setattr(operations, "migrate", candidate)
    monkeypatch.setattr(migration_cli, "_sqlite_policy", lambda _configuration: object())
    arguments = ["--source", str(tmp_path / "source"), "--target", str(tmp_path / "candidate"), "--default-profile", "v2"]

    outcome = migration_cli.Run("migration-build", arguments)
    assert outcome.exit_code == 0
    assert outcome.payload["schema_version"] == "trader_data_layout_migration"
    assert outcome.payload["state"] == "candidate_verified"
    candidate.assert_called_once()
    assert not tuple(tmp_path.iterdir())

    candidate.side_effect = operations.MigrationError("migration_source_changed")
    failed = migration_cli.Run("migration-build", arguments)
    assert failed.exit_code == 1
    assert failed.payload["state"] == "failed"
    assert failed.payload["error_code"] == "migration_source_changed"


def test_migration_rollback_does_not_load_sqlite_configuration(monkeypatch, tmp_path: Path) -> None:
    from scripts.engineering.migration import operations

    restore = Mock()
    monkeypatch.setattr(operations, "rollback", restore)
    monkeypatch.setattr(migration_cli, "_sqlite_policy", Mock(side_effect=AssertionError("configuration loaded")))
    outcome = migration_cli.Run("migration-rollback", ["--target", str(tmp_path / "target"), "--backup", str(tmp_path / "backup")])
    assert outcome.exit_code == 0
    assert outcome.payload["state"] == "rolled_back"
    restore.assert_called_once_with(tmp_path / "target", tmp_path / "backup")
    assert not tuple(tmp_path.iterdir())


def test_migration_paths_still_reject_project_and_relative_inputs(tmp_path: Path) -> None:
    from scripts.engineering.migration import operations

    with pytest.raises(operations.MigrationError, match="outside_repository"):
        operations._external_path(PROJECT_ROOT / "data")
    with pytest.raises(operations.MigrationError, match="absolute_external_paths"):
        operations._external_path(Path("relative"))
    assert operations._external_path(tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize(
    "action,state,expected_code",
    (
        ("build", "completed", 0),
        ("build", "verified", 1),
        ("activate", "verified", 0),
        ("activate", "finalized", 0),
        ("activate", "rolled_back", 1),
        ("rollback", "rolled_back", 0),
        ("rollback", "verified", 1),
        ("finalize", "finalized", 0),
        ("finalize", "verified", 1),
    ),
)
def test_repack_requires_the_selected_action_completion_state(monkeypatch, tmp_path: Path, action, state, expected_code) -> None:
    from trader.download.infra.history_partition_repack_state import HistoryPartitionRepackStatus

    status = HistoryPartitionRepackStatus(action, state, "a" * 64, "a" * 64, 1, 1, 100, 90)
    coordinator = Mock()
    for method in (coordinator.build, coordinator.activate, coordinator.rollback, coordinator.finalize):
        method.return_value = status
    monkeypatch.setattr(repack, "_coordinator", lambda _args, _action: coordinator)
    arguments = ["--source", str(tmp_path / "history"), "--target", str(tmp_path / "history-repacked")]
    if action == "finalize":
        arguments.extend(("--training-root", str(tmp_path / "training"), "--memory-evidence", str(tmp_path / "memory.json")))

    outcome = repack.Run("history-repack-" + action, arguments)
    assert outcome.exit_code == expected_code
    assert outcome.payload["schema_version"] == "history_archive_repack_status"
    assert outcome.payload["state"] == state
    assert outcome.payload["status"] == ("passed" if expected_code == 0 else "failed")
    assert sum(method.call_count for method in (coordinator.build, coordinator.activate, coordinator.rollback, coordinator.finalize)) == 1
    assert not tuple(tmp_path.iterdir())
