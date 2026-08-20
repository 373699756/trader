from __future__ import annotations

import json
from pathlib import Path

import pytest

from trader.entrypoints.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[2]


def test_default_runtime_config_uses_the_v2_namespace() -> None:
    config = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))

    assert config["runtime_dir"] == ".runtime/v2"


def test_cli_exposes_current_v2_maintenance_and_explicit_offline_research_commands() -> None:
    help_text = build_parser().format_help()

    for removed in ("migrate-v17", "recommendation-archive", "tomorrow-cutover-evidence"):
        assert removed not in help_text
    for retained in (
        "validate-config",
        "research-status",
        "research-history-download",
        "research-backtest",
        "research-r6-screen",
    ):
        assert retained in help_text


def test_run_script_exposes_read_only_research_status() -> None:
    shell = (ROOT / "run.sh").read_text(encoding="utf-8")

    assert "research-status" in shell


def test_run_script_exposes_explicit_offline_history_research_without_mapping_it_to_serve() -> None:
    shell = (ROOT / "run.sh").read_text(encoding="utf-8")

    assert "research-history-download" in shell
    assert "research-backtest" in shell
    assert "research-r6-screen" in shell
    assert "serve|app" in shell


def test_research_status_does_not_create_runtime_files(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert main(["--config", str(config), "research-status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["recorded_trade_dates"] == []
    assert payload["outcomes"]["initialized"] is False
    assert payload["score_r6_executable"] is False
    assert payload["score_r6_screening_executable"] is False
    assert payload["blockers"] == ["score_h0_archive_coverage_incomplete"]
    assert payload["promotion_blockers"] == ["score_r6_preregistered_forward_evidence_missing"]
    assert payload["schema_version"] == "v2_research_readiness_v2"
    assert payload["active_research"]["research_identity"] == "score_p0_v2"
    assert payload["active_research"]["historical_window"] == {
        "start": "2026-08-21",
        "end": "2026-10-23",
        "planned_trade_dates": 40,
        "recorded_trade_dates": 0,
    }
    assert payload["active_research"]["forward_window"] == {
        "start": "2026-10-26",
        "end": "2026-11-20",
        "planned_trade_dates": 20,
        "recorded_trade_dates": 0,
    }
    assert payload["legacy_research"]["research_identity"] == "score_p0_v1"
    assert not runtime_dir.exists()


def test_research_backtest_is_read_only_when_the_archive_does_not_exist(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert main(["--config", str(config), "research-backtest"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "insufficient_coverage"
    assert payload["archive"]["initialized"] is False
    assert len(payload["archive_manifest"]["content_hash"]) == 64
    assert len(payload["report_hash"]) == 64
    assert not runtime_dir.exists()


def test_research_r6_screen_refuses_to_freeze_without_h0_coverage(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert main(["--config", str(config), "research-r6-screen"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "insufficient_coverage"
    assert payload["historical_gate_passed"] is False
    assert payload["failure_reasons"] == ["score_h0_archive_coverage_incomplete"]
    assert not runtime_dir.exists()


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
