from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import trader.entrypoints.cli as cli_module
import trader.entrypoints.research_commands as research_commands
import trader.entrypoints.server as server_module
from trader.application.research.research_tomorrow_orchestrator import TomorrowResearchPrerequisite
from trader.entrypoints.cli import build_parser, main
from trader.entrypoints.server import build_parser as build_server_parser
from trader.infra.process_lock import ProcessLockError
from trader.infra.research.h1_point_in_time_archive import H1ArchiveConflictError
from trader.infra.research.tomorrow_research_artifacts import TomorrowResearchArtifactStoreError
from trader.infra.settings import load_runtime_settings

ROOT = Path(__file__).resolve().parents[2]


def _research_modules_loaded_by(module_name: str) -> set[str]:
    probe = (
        "import importlib,json,sys;"
        f"importlib.import_module({module_name!r});"
        "roots=('trader.application.research','trader.domain.research','trader.infra.research');"
        "print(json.dumps(sorted(name for name in sys.modules if name.startswith(roots))))"
    )
    completed = subprocess.run(
        (sys.executable, "-c", probe),
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    return set(json.loads(completed.stdout))


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
        "check",
        "download_history",
        "train-tomorrow",
        "validate-config",
        "performance-check",
        "research-status",
    ):
        assert retained in help_text
    for retired in ("research-history", "research-screen", "research-baostock-history", "serve", "app"):
        assert retired not in help_text


def test_cli_module_does_not_eagerly_load_research_implementations() -> None:
    assert _research_modules_loaded_by("trader.entrypoints.cli") == set()


def test_server_module_loads_only_authorized_background_research_consumers() -> None:
    allowed = {
        "trader.application.research",
        "trader.application.research.research_audit",
        "trader.application.research.research_coordination",
        "trader.application.research.v2_research_runtime",
    }

    assert _research_modules_loaded_by("trader.entrypoints.server") <= allowed


def test_server_entrypoint_accepts_only_the_two_typed_scoring_profiles() -> None:
    parser = build_server_parser()

    assert parser.parse_args(["--config", "/tmp/runtime.json", "--profile", "v1"]).profile == "v1"
    assert parser.parse_args(["--config", "/tmp/runtime.json", "--profile", "v2"]).profile == "v2"
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--config", "/tmp/runtime.json", "--profile", "latest"])
    assert error.value.code == 2


def test_performance_entrypoint_does_not_import_posix_resource_at_module_load() -> None:
    source = (ROOT / "src/trader/entrypoints/performance.py").read_text(encoding="utf-8")
    module = ast.parse(source)

    assert all(
        not (isinstance(statement, ast.Import) and any(alias.name == "resource" for alias in statement.names))
        for statement in module.body
    )


def test_run_script_exposes_only_the_aggregated_public_workflows() -> None:
    shell = (ROOT / "run.sh").read_text(encoding="utf-8")

    assert "check" in shell
    assert "download_history" in shell
    assert "train-tomorrow" in shell
    assert "research-r7-dossier" not in shell
    assert "serve|app" not in shell
    for internal_stage in (
        "validate-config",
        "research-status",
        "performance-check",
        "research-history",
        "research-screen",
        "research-baostock-history",
    ):
        assert internal_stage not in shell


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
    assert "./run.sh                         以默认 V1 启动本地 A 股研究看板" in completed.stdout
    assert "./run.sh --profile v2            显式使用 V2 启动" in completed.stdout
    assert "./run.sh check                   依次校验配置、研究状态和性能门禁" in completed.stdout
    assert "离线研究（仅在明确执行研究任务时使用）:" in completed.stdout
    assert "./run.sh download_history        下载/续传 BaoStock 历史日线归档" in completed.stdout
    assert "./run.sh train-tomorrow          从封存状态推导并连续运行可用 Tomorrow 训练阶段" in completed.stdout
    assert "research-r7-dossier" not in completed.stdout
    assert "所有命令都可追加 --profile v1|v2；未指定时为 V1" in completed.stdout
    assert "./run.sh serve" not in completed.stdout
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
    assert completed.stdout == f"server:--config {config} --profile v1\n"


@pytest.mark.parametrize("arguments", (("--profile", "v2"), ("--profile=v2",)))
def test_run_script_accepts_an_explicit_v2_profile_without_a_serve_alias(
    tmp_path: Path,
    arguments: tuple[str, ...],
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-server", "printf 'server:%s\\n' \"$*\"")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), *arguments),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(venv_bin.parent), "TRADER_CONFIG": str(config)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"server:--config {config} --profile v2\n"


def test_run_script_forwards_baostock_download_arguments_after_normalizing_the_profile(tmp_path: Path) -> None:
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
            "download_history",
            "--runtime-dir",
            str(tmp_path / "outside"),
            "--sessions",
            "3",
            "--profile",
            "v2",
        ),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(venv_bin.parent), "TRADER_CONFIG": str(config)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == (
        f"cli:--config {config} --profile v2 download_history --runtime-dir {tmp_path / 'outside'} --sessions 3\n"
    )


def test_run_script_forwards_the_single_tomorrow_training_command_without_stage_arguments(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-server", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-cli", "printf 'cli:%s\\n' \"$*\"")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "train-tomorrow"),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(venv_bin.parent), "TRADER_CONFIG": str(config)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"cli:--config {config} --profile v1 train-tomorrow\n"


def test_run_script_rejects_an_unknown_profile_before_environment_setup(tmp_path: Path) -> None:
    missing_venv = tmp_path / "missing-venv"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "--profile", "latest"),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(missing_venv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "评分档位只能是 v1 或 v2: latest\n"
    assert not missing_venv.exists()


@pytest.mark.parametrize(
    ("command", "extra", "expected_stages"),
    (("check", (), ("validate-config", "research-status", "performance-check")),),
)
def test_cli_aggregates_all_stages_and_preserves_nonzero_gate_results(
    command: str,
    extra: tuple[str, ...],
    expected_stages: tuple[str, ...],
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_stage(argv: list[str]) -> int:
        calls.append(argv)
        return 1 if len(calls) == 1 else 0

    monkeypatch.setattr(cli_module, "_run_group_stage", fake_stage)
    config = ROOT / "config/v2/runtime.json"

    assert main(["--config", str(config), "--profile", "v2", command, *extra]) == 1

    assert [call[-1] if "--workers" not in call else call[-3] for call in calls] == list(expected_stages)
    assert all(call[:4] == ["--config", str(config), "--profile", "v2"] for call in calls)
    summary = json.loads(capsys.readouterr().out)
    assert summary == {
        "command": command,
        "profile": "v2",
        "schema_version": "trader_command_group_v1",
        "stages": [
            {"command": stage, "exit_code": 1 if index == 0 else 0} for index, stage in enumerate(expected_stages)
        ],
        "status": "completed_with_failures",
    }


def test_powershell_help_uses_the_same_command_groups() -> None:
    powershell = (ROOT / "run.ps1").read_text(encoding="utf-8")

    assert "日常使用（不做离线研究）:" in powershell
    assert "离线研究（仅在明确执行研究任务时使用）:" in powershell
    assert ".\\run.ps1 download_history        下载/续传 BaoStock 历史日线归档" in powershell
    assert "research-history" not in powershell
    assert "research-screen" not in powershell
    assert ".\\run.ps1 train-tomorrow          从封存状态推导并连续运行可用 Tomorrow 训练阶段" in powershell
    assert "所有命令都可追加 --profile v1|v2；未指定时为 V1" in powershell


def test_research_status_is_historical_only_and_does_not_create_runtime_files(tmp_path: Path, capsys) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    assert main(["--config", str(config), "research-status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "v2_research_readiness_v9"
    assert payload["validation_mode"] == "historical_only"
    assert payload["recorded_trade_dates"] == []
    assert payload["outcomes"]["initialized"] is False
    assert payload["score_r6_executable"] is False
    assert payload["blockers"] == ["score_h0_archive_coverage_incomplete"]
    assert payload["tomorrow_p2"]["validation_mode"] == "historical_only"
    assert payload["tomorrow_v2_historical_risk"] == {
        "model_artifact_hash": "",
        "production_authority": False,
        "report_hash": "",
        "status": "not_run",
    }
    assert payload["tomorrow_research"]["status"] == "blocked"
    assert payload["tomorrow_research"]["run_id"] is None
    assert payload["tomorrow_research"]["next_stage"] == "resource_probe"
    assert payload["tomorrow_research"]["input_prerequisite_status"] == "blocked"
    assert len(payload["tomorrow_research"]["input_prerequisite_hash"]) == 64
    assert payload["tomorrow_research"]["input_blockers"] == [
        "tomorrow_common_trading_days_below_1000",
        "tomorrow_h1_historical_data_insufficient",
        "tomorrow_terminal_holdout_below_200",
    ]
    assert payload["tomorrow_research"]["production_authority"] is False
    assert payload["retired_research"] == [
        {
            "blocker": "historical_point_in_time_missing",
            "research_identity": "score_p0_v1",
            "status": "historical_rejected",
        },
        {
            "blocker": "fixed_historical_dates_missed",
            "research_identity": "score_p0_v2",
            "status": "historical_collection_failed",
        },
    ]
    for retired in ("active_research", "promotion_blockers", "score_r7", "tomorrow_profile_comparison"):
        assert retired not in payload
    assert not runtime_dir.exists()


def test_train_tomorrow_runs_a_prerequisite_before_resource_handoff_without_creating_v3(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime_dir = tmp_path / "runtime"
    runtime["runtime_dir"] = str(runtime_dir)
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        monkeypatch.delenv(name, raising=False)

    assert main(["--config", str(config), "train-tomorrow"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["next_stage"] == "resource_probe"
    assert payload["blockers"] == [
        "tomorrow_common_trading_days_below_1000",
        "tomorrow_h1_historical_data_insufficient",
        "tomorrow_terminal_holdout_below_200",
    ]
    assert len(payload["input_prerequisite_hash"]) == 64
    assert payload["production_readiness"]["status"] == "production_adaptation_blocked"
    assert payload["production_authority"] is False
    assert {os.environ[name] for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS")} == {"2"}
    assert not runtime_dir.exists()


def test_research_status_keeps_tomorrow_graph_conflict_out_of_h1_input_blockers(tmp_path: Path, monkeypatch) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    class _ReadyPrerequisite:
        def inspect(self) -> TomorrowResearchPrerequisite:
            return TomorrowResearchPrerequisite("ready", "a" * 64)

    monkeypatch.setattr(research_commands, "_tomorrow_research_prerequisite", lambda _runtime: _ReadyPrerequisite())
    monkeypatch.setattr(
        research_commands.TomorrowResearchArtifactStore,
        "load_graph",
        lambda _store: (_ for _ in ()).throw(TomorrowResearchArtifactStoreError("graph invalid")),
    )

    result = research_commands._read_tomorrow_research_status(load_runtime_settings(config))

    assert result["status"] == "artifact_conflict"
    assert result["input_prerequisite_status"] == "ready"
    assert result["input_prerequisite_hash"] == "a" * 64
    assert result["input_blockers"] == []
    assert result["production_blockers"] == ["tomorrow_research_artifact_invalid"]


def test_research_status_reports_h1_conflict_as_the_input_boundary(tmp_path: Path, monkeypatch) -> None:
    runtime = json.loads((ROOT / "config/v2/runtime.json").read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    config = tmp_path / "runtime.json"
    config.write_text(json.dumps(runtime), encoding="utf-8")

    class _BrokenPrerequisite:
        def inspect(self) -> TomorrowResearchPrerequisite:
            raise H1ArchiveConflictError("H1 archive invalid")

    monkeypatch.setattr(research_commands, "_tomorrow_research_prerequisite", lambda _runtime: _BrokenPrerequisite())

    result = research_commands._read_tomorrow_research_status(load_runtime_settings(config))

    assert result["status"] == "artifact_conflict"
    assert result["input_prerequisite_status"] == "artifact_conflict"
    assert result["input_prerequisite_hash"] == ""
    assert result["input_blockers"] == ["h1_archive_invalid"]
    assert result["production_blockers"] == ["tomorrow_research_artifact_invalid"]


@pytest.mark.parametrize(
    "command",
    (
        "migrate-v17",
        "recommendation-archive",
        "tomorrow-cutover-evidence",
        "research-r7-dossier",
        "research-tomorrow-profile-report",
        "research-history",
        "research-screen",
        "research-baostock-history",
        "research-history-download",
        "research-backtest",
        "research-r6-screen",
        "research-r6-daily-screen",
        "research-r6-stability-screen",
        "research-tomorrow-p2-screen",
        "research-tomorrow-v1-v2-holdout",
        "research-tomorrow-v2-risk-validation",
        "serve",
        "app",
    ),
)
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


def test_server_lock_conflict_explains_the_existing_service_and_safe_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class _LockedProcess:
        def __init__(self, _path: Path) -> None:
            pass

        def acquire(self) -> None:
            raise ProcessLockError(f"trader-server is already running for {tmp_path}")

    settings = SimpleNamespace(
        runtime_dir=tmp_path,
        server=SimpleNamespace(host="127.0.0.1", port=5050, allow_insecure_non_loopback=False),
    )
    monkeypatch.setattr(
        server_module,
        "build_system",
        lambda _config, *, tomorrow_scoring_profile: SimpleNamespace(settings=settings),
    )
    monkeypatch.setattr(server_module, "ProcessLock", _LockedProcess)

    with pytest.raises(SystemExit) as error:
        server_module.main(["--config", str(tmp_path / "runtime.json"), "--profile", "v1"])

    message = str(error.value)
    assert "trader-server is already running" in message
    assert "现有服务地址->http://127.0.0.1:5050" in message
    assert "请在原启动终端按 Ctrl+C 正常停止后，再运行原启动命令（./run.sh 或 .\\run.ps1）" in message
    assert "不要删除 server.lock" in message


def test_v2_budget_uses_its_own_database_and_never_opens_the_legacy_runtime_name() -> None:
    bootstrap = (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")

    assert 'settings.runtime_dir / "deepseek-budget.sqlite3"' in bootstrap
    assert 'settings.runtime_dir / "runtime.sqlite3"' not in bootstrap
