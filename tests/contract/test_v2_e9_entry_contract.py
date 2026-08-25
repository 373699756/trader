from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from trader.entrypoints.cli import build_parser, main

ROOT = Path(__file__).resolve().parents[2]


def _write_fake_entrypoint(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    newer = (ROOT / "pyproject.toml").stat().st_mtime + 10.0
    os.utime(path, (newer, newer))


def test_default_runtime_config_uses_the_v2_namespace() -> None:
    config = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))

    assert config["runtime_dir"] == ".runtime/v2"


def test_cli_exposes_current_v2_maintenance_and_explicit_offline_research_commands() -> None:
    help_text = build_parser().format_help()

    for removed in ("migrate-v17", "recommendation-archive", "tomorrow-cutover-evidence"):
        assert removed not in help_text
    for retained in (
        "validate-config",
        "performance-check",
        "research-status",
        "research-history-download",
        "research-backtest",
        "research-r6-screen",
        "research-r6-daily-screen",
        "research-r6-stability-screen",
        "research-r7-dossier",
    ):
        assert retained in help_text


def test_performance_entrypoint_does_not_import_posix_resource_at_module_load() -> None:
    source = (ROOT / "src/trader/entrypoints/performance.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    assert all(
        not (isinstance(statement, ast.Import) and any(alias.name == "resource" for alias in statement.names))
        for statement in module.body
    )


def test_run_script_exposes_read_only_research_status() -> None:
    shell = (ROOT / "run.sh").read_text(encoding="utf-8")

    assert "research-status" in shell
    assert "performance-check" in shell


def test_run_script_exposes_explicit_offline_history_research_without_mapping_it_to_serve() -> None:
    shell = (ROOT / "run.sh").read_text(encoding="utf-8")

    assert "research-history-download" in shell
    assert "research-backtest" in shell
    assert "research-r6-screen" in shell
    assert "research-r6-daily-screen" in shell
    assert "research-r6-stability-screen" in shell
    assert "research-r7-dossier" in shell
    assert "serve|app" in shell


def test_run_script_help_separates_daily_commands_from_offline_research(tmp_path: Path) -> None:
    missing_venv = tmp_path / "missing-venv"
    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "help"),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(missing_venv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "日常使用（不做离线研究）:" in completed.stdout
    assert "./run.sh                         启动本地 A 股研究看板（推荐）" in completed.stdout
    assert "./run.sh validate-config         校验配置后退出，不启动服务" in completed.stdout
    assert "./run.sh research-status         只读查看研究数据准备状态" in completed.stdout
    assert "./run.sh performance-check       离线运行活动生产函数性能门禁" in completed.stdout
    assert "离线研究（仅在明确执行研究任务时使用）:" in completed.stdout
    assert "./run.sh research-history-download        下载并续传离线历史日线归档" in completed.stdout
    assert "research-r7-dossier --research-identity <ID>" in completed.stdout
    assert "日常启动不需要填写任何参数" in completed.stdout
    assert "用法: ./run.sh [serve|" not in completed.stdout
    assert not missing_venv.exists()


def test_run_script_unknown_command_fails_before_environment_setup_with_concise_guidance(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("python", "trader-server"):
        _write_fake_entrypoint(venv_bin / name, "exit 99")

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "serv"),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(venv_bin.parent)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ("未知命令: serv\n日常启动直接运行: ./run.sh\n查看全部命令: ./run.sh help\n")


def test_run_script_without_arguments_still_starts_the_dashboard(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    server = venv_bin / "trader-server"
    _write_fake_entrypoint(python, "exit 99")
    _write_fake_entrypoint(server, "printf 'server:%s\\n' \"$*\"")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh")),
        cwd=ROOT,
        env={
            **os.environ,
            "VENV_DIR": str(venv_bin.parent),
            "TRADER_CONFIG": str(config),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"server:--config {config}\n"


def test_run_script_preserves_offline_research_argument_forwarding(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-server", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-cli", "printf 'cli:%s\\n' \"$*\"")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        (
            "bash",
            str(ROOT / "run.sh"),
            "research-r7-dossier",
            "--research-identity",
            "score_r6_forward_test",
        ),
        cwd=ROOT,
        env={
            **os.environ,
            "VENV_DIR": str(venv_bin.parent),
            "TRADER_CONFIG": str(config),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == (
        f"cli:--config {config} research-r7-dossier --research-identity score_r6_forward_test\n"
    )


def test_powershell_help_uses_the_same_command_groups() -> None:
    powershell = (ROOT / "run.ps1").read_text(encoding="utf-8")

    assert "日常使用（不做离线研究）:" in powershell
    assert "离线研究（仅在明确执行研究任务时使用）:" in powershell
    assert "日常启动不需要填写任何参数" in powershell


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
    assert payload["score_r6_daily"] == {
        "failure_reasons": [],
        "historical_gate_passed": False,
        "promotion_authority": False,
        "report_hash": "",
        "selected_candidate_hash": "",
        "status": "not_run",
    }
    assert payload["score_r6_stability"] == {
        "diagnostic_gate_passed": False,
        "evidence_class": "reused_observed_validation_window",
        "failure_reasons": [],
        "promotion_authority": False,
        "report_hash": "",
        "selected_candidate_hash": "",
        "status": "not_run",
    }
    assert payload["blockers"] == ["score_h0_archive_coverage_incomplete"]
    assert payload["promotion_blockers"] == ["score_r6_preregistered_forward_evidence_missing"]
    assert payload["score_r7"] == {"dossier_count": 0, "dossiers": []}
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


def test_research_r6_daily_screen_refuses_to_freeze_without_h0_coverage(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert main(["--config", str(config), "research-r6-daily-screen"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "insufficient_coverage"
    assert payload["historical_gate_passed"] is False
    assert payload["failure_reasons"] == ["score_h0_archive_coverage_incomplete"]
    assert payload["promotion_authority"] is False
    assert not runtime_dir.exists()


def test_research_r6_stability_screen_fails_closed_without_the_bound_parent(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert main(["--config", str(config), "research-r6-stability-screen"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "parent_mismatch"
    assert payload["diagnostic_gate_passed"] is False
    assert payload["failure_reasons"] == ["score_r6_daily_parent_artifact_mismatch"]
    assert payload["promotion_authority"] is False
    assert not (runtime_dir / "score-r6-stability").exists()


def test_research_r7_dossier_fails_closed_without_eligible_evidence(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert (
        main(
            [
                "--config",
                str(config),
                "research-r7-dossier",
                "--research-identity",
                "score_r6_forward_20261201_v1",
            ]
        )
        == 1
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"reason": "score_r7_evidence_invalid", "status": "blocked"}
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
