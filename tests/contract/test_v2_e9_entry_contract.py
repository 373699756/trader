from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.entrypoints.cli import build_parser

ROOT = Path(__file__).resolve().parents[2]


def test_default_runtime_config_uses_the_v2_namespace() -> None:
    config = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))

    assert config["runtime_dir"] == ".runtime/v2"


def test_cli_exposes_only_current_v2_maintenance_commands() -> None:
    help_text = build_parser().format_help()

    for removed in ("migrate-v17", "recommendation-archive", "tomorrow-cutover-evidence"):
        assert removed not in help_text
    for retained in ("validate-config",):
        assert retained in help_text


@pytest.mark.parametrize("command", ("migrate-v17", "recommendation-archive", "tomorrow-cutover-evidence"))
def test_removed_legacy_cli_commands_are_rejected(command: str) -> None:
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([command])
    assert error.value.code == 2


def test_start_scripts_do_not_map_legacy_environment_names() -> None:
    shell = (ROOT / "run.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "run.ps1").read_text(encoding="utf-8")

    assert "${HOST" not in shell
    assert "${PORT" not in shell
    assert "$env:HOST" not in powershell
    assert "$env:PORT" not in powershell
    assert "TRADER_HOST" in shell and "TRADER_PORT" in shell
    assert "TRADER_HOST" in powershell and "TRADER_PORT" in powershell


def test_entrypoints_and_lock_are_runtime_directory_relative() -> None:
    server = (ROOT / "src/trader/entrypoints/server.py").read_text(encoding="utf-8")
    lock = (ROOT / "src/trader/infra/process_lock.py").read_text(encoding="utf-8")

    assert 'ProcessLock(system.settings.runtime_dir / "server.lock")' in server
    assert "class ProcessLock" in lock
    assert ".runtime/v17" not in server
    assert ".runtime/v17" not in lock


def test_v2_budget_uses_its_own_database_and_never_opens_the_legacy_runtime_name() -> None:
    bootstrap = (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")

    assert 'settings.runtime_dir / "deepseek-budget.sqlite3"' in bootstrap
    assert 'settings.runtime_dir / "runtime.sqlite3"' not in bootstrap
