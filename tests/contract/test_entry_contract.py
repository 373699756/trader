from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import trader.entrypoints.server as server_module
import trader.training.entrypoints.commands as research_commands
from trader.entrypoints.cli import BuildParser, Main
from trader.entrypoints.server import BuildParser as build_server_parser
from trader.infra.process_lock import ProcessLockError
from trader.infra.settings import LoadRuntimeSettings
from trader.training.application.tomorrow_research_orchestrator import TomorrowResearchPrerequisiteStatus
from trader.training.infra.research.h1_point_in_time_snapshot import H1PointInTimeSnapshotConflictError
from trader.training.infra.research.tomorrow_research_evidence import TomorrowResearchEvidenceIndexError

ROOT = Path(__file__).resolve().parents[2]


def test_server_import_for_spawn_does_not_load_composition_or_model_stack() -> None:
    result = subprocess.run(
        [sys.executable, "-c", (
            "import sys; import trader.entrypoints.server; "
            "assert 'trader.bootstrap' not in sys.modules; "
            "assert 'numpy' not in sys.modules; assert 'torch' not in sys.modules"
        )],
        capture_output=True, text=True, timeout=15, check=False,
    )
    assert result.returncode == 0, result.stderr


def _runtime_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    shutil.copytree(ROOT / "config", config_dir, dirs_exist_ok=True)
    path = config_dir / "runtime.json"
    runtime = json.loads(path.read_text(encoding="utf-8"))
    runtime["runtime_dir"] = str(tmp_path / "runtime")
    path.write_text(json.dumps(runtime), encoding="utf-8")
    return path


def _research_modules_loaded_by(module_name: str) -> set[str]:
    probe = (
        "import importlib,json,sys;"
        f"importlib.import_module({module_name!r});"
        "roots=('trader.training.application','trader.training.domain.evaluation','trader.training.infra.research');"
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


def test_default_runtime_config_uses_the_canonical_namespace() -> None:
    config = json.loads((ROOT / "config/runtime.json").read_text(encoding="utf-8"))

    assert config["runtime_dir"] == ".runtime/trader"


def test_cli_exposes_current_maintenance_and_explicit_offline_research_commands() -> None:
    help_text = BuildParser().format_help()

    for removed in ("migrate-v17", "recommendation-archive", "tomorrow-cutover-evidence"):
        assert removed not in help_text
    for retained in (
        "sync",
        "train",
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
        "trader.training.application",
        "trader.training.application.research_audit",
        "trader.training.application.research_coordination",
        "trader.training.application.research_runtime",
        "trader.training.application.tomorrow_training",
        "trader.training.application.baseline_replay_report",
        "trader.training.application.challenger_replay_report",
        "trader.training.application.historical_extraction_models",
        "trader.training.application.json_values",
        "trader.training.application.research_ports",
        "trader.training.domain.evaluation",
        "trader.training.domain.evaluation.research_identity",
        "trader.training.domain.evaluation.challengers",
        "trader.training.domain.evaluation.historical",
        "trader.training.domain.evaluation.specification",
        "trader.training.infra.research",
        "trader.recommendation.infra.persistence.trace_schema",
        "trader.recommendation.infra.persistence.trace_codec",
        "trader.infra.serialization.research_trace_validation",
        "trader.recommendation.infra.persistence.trace_projection",
        "trader.recommendation.infra.persistence.outcome_evidence_index",
        "trader.recommendation.infra.persistence.freeze_trace_index",
        "trader.recommendation.infra.persistence.trace_capacity",
    }

    assert _research_modules_loaded_by("trader.entrypoints.server") <= allowed


def test_server_entrypoint_accepts_production_and_shadow_profile_selections() -> None:
    parser = build_server_parser()

    assert parser.parse_args(["--config", "/tmp/runtime.json", "v1"]).profiles == ("v1",)
    assert parser.parse_args(["--config", "/tmp/runtime.json", "v2"]).profiles == ("v2",)
    assert parser.parse_args(["--config", "/tmp/runtime.json", "v5"]).profiles == ("v5",)
    assert parser.parse_args(["--config", "/tmp/runtime.json"]).profiles == ("v1", "v2", "v3", "v5")
    assert parser.parse_args(["--config", "/tmp/runtime.json", "v1,v2,v3"]).profiles == ("v1", "v2", "v3")
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["--config", "/tmp/runtime.json", "latest"])
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

    assert "./run.sh check" not in shell
    assert "train" in shell
    assert "sync" in shell
    assert "download_risk" not in shell
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
    assert "./run.sh                         同时启动全部档位 V1、V2、V3、V5" in completed.stdout
    assert "./run.sh v2                      只启动 V2" in completed.stdout
    assert "./run.sh v1,v2,v3                同时启动所列档位，Web按钮互斥切换" in completed.stdout
    assert "scripts/diagnose_runtime.py --profile local" in completed.stdout
    assert "离线研究（仅在明确执行研究任务时使用）:" in completed.stdout
    assert "./run.sh train                   历史维护后处理全部档位 V1、V2、V3、V5" in completed.stdout
    assert "--allow-partial-history" not in completed.stdout
    assert "research-r7-dossier" not in completed.stdout
    assert "档位以逗号分隔，可自由组合；省略时选择全部。" in completed.stdout
    assert "./run.sh serve" not in completed.stdout
    assert not missing_venv.exists()


def test_run_script_unknown_command_fails_before_environment_setup_with_concise_guidance(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    for name in ("python3", "trader-server"):
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


@pytest.mark.parametrize(
    "arguments",
    (
        ("train", "--sessions", "2000"),
        ("train", "--mode", "update"),
        ("--profile", "v2", "train"),
    ),
)
def test_run_script_rejects_download_arguments_before_environment_setup(
    arguments: tuple[str, ...], tmp_path: Path
) -> None:
    missing_venv = tmp_path / "must-not-exist"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), *arguments),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(missing_venv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("用法：./run.sh" if arguments[0] == "train" else "未知命令: --profile")
    assert not missing_venv.exists()


@pytest.mark.parametrize("command", ("install-history-automation", "uninstall-history-automation"))
def test_run_script_rejects_retired_automation_commands_before_environment_setup(command: str, tmp_path: Path) -> None:
    missing_venv = tmp_path / "must-not-exist"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), command),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(missing_venv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == (f"未知命令: {command}\n日常启动直接运行: ./run.sh\n查看全部命令: ./run.sh help\n")
    assert not missing_venv.exists()


def test_run_script_without_arguments_still_starts_the_dashboard(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python3"
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


def test_run_script_repairs_a_moved_virtual_environment_before_starting(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python3"
    server = venv_bin / "trader-server"
    _write_fake_entrypoint(
        python,
        """if [[ "$*" == *"--no-build-isolation"* ]]; then
  exit 98
fi
if [[ "$*" == *"-m pip install"* ]]; then
  printf '#!/usr/bin/env bash\\nprintf "server:%%s\\\\n" "$*"\\n' > "$REPAIR_ENTRYPOINT"
  chmod +x "$REPAIR_ENTRYPOINT"
  exit 0
fi
exit 99""",
    )
    server.write_text("#!/missing/moved-venv/python\n", encoding="utf-8")
    server.chmod(0o755)
    newer = (ROOT / "pyproject.toml").stat().st_mtime + 10.0
    os.utime(server, (newer, newer))
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh")),
        cwd=ROOT,
        env={
            **os.environ,
            "VENV_DIR": str(venv_bin.parent),
            "TRADER_CONFIG": str(config),
            "REPAIR_ENTRYPOINT": str(server),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"server:--config {config}\n"


@pytest.mark.parametrize(
    ("arguments", "profile"),
    (
        (("v2",), "v2"),
        (("v1,v2,v3",), "v1,v2,v3"),
        (("v3",), "v3"),
        (("v1,v2,v3,v5",), "v1,v2,v3,v5"),
        (("v1",), "v1"),
    ),
)
def test_run_script_accepts_an_explicit_profile_without_a_serve_alias(
    tmp_path: Path,
    arguments: tuple[str, ...],
    profile: str,
) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python3", "exit 99")
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
    assert completed.stdout == f"server:--config {config} {profile}\n"


@pytest.mark.parametrize("profile_args", ((), ("v1",), ("v1,v5",), ("v2",), ("v2,v3",)))
def test_run_script_forwards_default_or_explicit_v1_training(tmp_path: Path, profile_args: tuple[str, ...]) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python3", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-server", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-cli", "printf 'cli:%s\\n' \"$*\"")
    _write_fake_entrypoint(venv_bin / "uname", "printf 'Darwin\\n'")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "train", *profile_args),
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{venv_bin}:{os.environ['PATH']}",
            "VENV_DIR": str(venv_bin.parent),
            "TRADER_CONFIG": str(config),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    suffix = f" {profile_args[0]}" if profile_args else ""
    assert completed.stdout == f"cli:--config {config} train{suffix}\n"


@pytest.mark.parametrize("download_result", (0, 1, 130))
def test_train_stops_before_training_on_history_failure(monkeypatch, download_result):
    from trader.bootstrap import ExecuteDownloadTrain

    calls = []
    monkeypatch.setattr(research_commands, "ConfigureTrainingResources", lambda **_kwargs: None)
    monkeypatch.setattr("trader.download.entrypoints.commands.RunDownload", lambda *args, **kwargs: download_result)
    monkeypatch.setattr(
        "trader.bootstrap.ExecuteSelectedTraining", lambda runtime, profiles: calls.append(runtime) or 7
    )
    assert ExecuteDownloadTrain(ROOT / "config/runtime.json", scoring_profiles=("v2", "v3")) == (download_result or 7)
    assert len(calls) == int(download_result == 0)


def test_train_uses_one_bounded_linux_scope(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python3", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-server", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-cli", "exit 0")
    _write_fake_entrypoint(venv_bin / "uname", "printf 'Linux\\n'")
    _write_fake_entrypoint(venv_bin / "systemctl", "exit 0")
    _write_fake_entrypoint(venv_bin / "systemd-run", "printf 'scope:%s\\n' \"$*\"")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "train"),
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{venv_bin}:{os.environ['PATH']}",
            "VENV_DIR": str(venv_bin.parent),
            "TRADER_CONFIG": str(config),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert "--property=MemoryHigh=4608M" in completed.stdout
    assert "--property=MemoryMax=5120M" in completed.stdout
    assert completed.stdout.rstrip().endswith(f"-- {venv_bin / 'trader-cli'} --config {config} train")


def test_run_script_warns_when_history_download_scope_is_unavailable(tmp_path: Path) -> None:
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_fake_entrypoint(venv_bin / "python3", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-server", "exit 99")
    _write_fake_entrypoint(venv_bin / "trader-cli", "printf 'cli:%s\\n' \"$*\"")
    _write_fake_entrypoint(venv_bin / "uname", "printf 'Linux\\n'")
    _write_fake_entrypoint(venv_bin / "systemctl", "exit 1")
    config = tmp_path / "runtime.json"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "train"),
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{venv_bin}:{os.environ['PATH']}",
            "VENV_DIR": str(venv_bin.parent),
            "TRADER_CONFIG": str(config),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == f"cli:--config {config} train\n"
    assert "不能提供 cgroup 硬隔离" in completed.stderr


def test_run_script_rejects_an_unknown_profile_before_environment_setup(tmp_path: Path) -> None:
    missing_venv = tmp_path / "missing-venv"

    completed = subprocess.run(
        ("bash", str(ROOT / "run.sh"), "v9"),
        cwd=ROOT,
        env={**os.environ, "VENV_DIR": str(missing_venv)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == "档位列表只能包含 v1、v2、v3、v5，以逗号分隔且不得重复。\n"
    assert not missing_venv.exists()


@pytest.mark.parametrize("command", ("check", "download_train", "download_qfq", "download_risk", "download_scheduled"))
def test_removed_data_commands_are_rejected_before_configuration(command):
    with pytest.raises(SystemExit) as error:
        Main(["--config", "/unavailable/runtime.json", command])
    assert error.value.code == 2


def test_powershell_help_uses_the_same_command_groups() -> None:
    powershell = (ROOT / "run.ps1").read_text(encoding="utf-8")

    assert "日常使用（不做离线研究）:" in powershell
    assert "离线研究（仅在明确执行研究任务时使用）:" in powershell
    assert ".\\run.ps1                         同时启动全部档位 V1、V2、V3、V5" in powershell
    assert ".\\run.ps1 v1,v2,v3                同时启动所列档位，Web按钮互斥切换" in powershell
    assert "research-history" not in powershell
    assert "research-screen" not in powershell
    assert "--allow-partial-history" not in powershell
    assert "档位以逗号分隔，可自由组合；省略时选择全部。" in powershell
    assert "& $SelectedEntryPoint --help" in powershell
    assert "$ForwardArgs = @($Selection)" in powershell
    assert "^v(1|2|3|5)(,v(1|2|3|5))*$" in powershell
    assert "config\\runtime.json" in powershell
    assert "config\\v2\\runtime.json" not in powershell
    assert '$PublicModes = @("help", "-h", "--help", "sync", "train")' in powershell
    assert "install-history-automation" not in powershell
    assert "uninstall-history-automation" not in powershell


def test_research_status_is_historical_only_and_does_not_create_runtime_files(
    tmp_path: Path,
    capsys,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_dir = tmp_path / "runtime"
    config = _runtime_config(tmp_path)
    research_root = LoadRuntimeSettings(config).data_paths.research("v2")
    research_root.mkdir(parents=True)
    legacy_database = research_root / "samples.sqlite3"
    legacy_database.write_bytes(b"retired shared research database")

    assert Main(["--config", str(config), "research-status"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == "research_readiness"
    assert payload["production_authority"] is False
    assert payload["validation_mode"] == "historical_only"
    assert payload["history_archive"] == {
        "snapshot_hash": None,
        "calendar_sessions": 0,
        "data_cutoff": None,
        "label_cutoff": None,
        "partition_count": 0,
        "point_in_time_parity": False,
        "production_authority": False,
        "reason": "history_snapshot_unavailable",
        "state": "unavailable",
        "universe_count": 0,
    }
    assert "baostock_history" not in payload
    assert payload["recorded_trade_dates"] == []
    assert payload["outcomes"]["initialized"] is False
    assert payload["blockers"] == ["target_selection_external_validation_required"]
    assert payload["target_selection"]["research_identity"] == "eleven_layer_full_population"
    assert payload["target_selection"]["strategies"] == ["tomorrow", "d25"]
    assert legacy_database.read_bytes() == b"retired shared research database"
    assert not (research_root / "experiments/historical_screening/samples.sqlite3").exists()
    assert not (research_root / "experiments/h1_point_in_time/samples.sqlite3").exists()
    assert payload["historical_screening"]["target_selection_evidence"] is False
    assert payload["tomorrow_historical"]["validation_mode"] == "historical_only"
    assert payload["tomorrow_historical_risk"] == {
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
            "research_identity": "historical_research_baseline",
            "status": "historical_rejected",
        },
        {
            "blocker": "fixed_historical_dates_missed",
            "research_identity": "preregistered_research",
            "status": "historical_collection_failed",
        },
    ]
    for retired in ("active_research", "promotion_blockers", "score_r7", "tomorrow_profile_comparison"):
        assert retired not in payload
    assert not runtime_dir.exists()


def test_tomorrow_training_resource_policy_is_two_threads_and_lower_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []
    monkeypatch.setattr(research_commands, "_TRAINING_PRIORITY_LOWERED", False)
    monkeypatch.setattr(research_commands.os, "name", "posix")
    monkeypatch.setattr(research_commands.os, "nice", observed.append)

    research_commands.ConfigureTrainingResources()
    research_commands.ConfigureTrainingResources()

    assert observed == [10]
    assert {
        os.environ[name]
        for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")
    } == {"2"}


def test_research_status_keeps_tomorrow_graph_conflict_out_of_h1_input_blockers(tmp_path: Path, monkeypatch) -> None:
    config = _runtime_config(tmp_path)

    class _ReadyPrerequisite:
        def inspect(self) -> TomorrowResearchPrerequisiteStatus:
            return TomorrowResearchPrerequisiteStatus("ready", "a" * 64)

    monkeypatch.setattr(research_commands, "_tomorrow_research_prerequisite", lambda _runtime: _ReadyPrerequisite())
    monkeypatch.setattr(
        research_commands.TomorrowResearchEvidenceIndex,
        "load_graph",
        lambda _repository: (_ for _ in ()).throw(TomorrowResearchEvidenceIndexError("graph invalid")),
    )

    result = research_commands._read_tomorrow_research_status(LoadRuntimeSettings(config).data_paths.research("v2"))

    assert result["status"] == "artifact_conflict"
    assert result["input_prerequisite_status"] == "ready"
    assert result["input_prerequisite_hash"] == "a" * 64
    assert result["input_blockers"] == []
    assert result["production_blockers"] == ["tomorrow_research_artifact_invalid"]


def test_research_status_reports_h1_conflict_as_the_input_boundary(tmp_path: Path, monkeypatch) -> None:
    config = _runtime_config(tmp_path)

    class _BrokenPrerequisite:
        def inspect(self) -> TomorrowResearchPrerequisiteStatus:
            raise H1PointInTimeSnapshotConflictError("H1 archive invalid")

    monkeypatch.setattr(research_commands, "_tomorrow_research_prerequisite", lambda _runtime: _BrokenPrerequisite())

    result = research_commands._read_tomorrow_research_status(LoadRuntimeSettings(config).data_paths.research("v2"))

    assert result["status"] == "artifact_conflict"
    assert result["input_prerequisite_status"] == "artifact_conflict"
    assert result["input_prerequisite_hash"] == ""
    assert result["input_blockers"] == ["h1_archive_invalid"]
    assert result["production_blockers"] == ["tomorrow_research_artifact_invalid"]


@pytest.mark.parametrize(
    "command",
    (
        "download",
        "download-train",
        "qfq_download",
        "risk_download",
        "train-v2",
        "train-v3",
        "train-both",
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
        BuildParser().parse_args([command])
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

    assert 'ProcessLock(settings.runtime_dir / "server.lock")' in server
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

    def forbidden_build(*args, **kwargs):
        raise AssertionError("duplicate server must be rejected before loading resources")

    monkeypatch.setattr("trader.bootstrap.BuildSystem", forbidden_build)
    monkeypatch.setattr(server_module, "LoadRuntimeSettings", lambda _path: settings)
    monkeypatch.setattr(server_module, "ProcessLock", _LockedProcess)

    with pytest.raises(SystemExit) as error:
        server_module.Main(["--config", str(tmp_path / "runtime.json"), "v2"])

    message = str(error.value)
    assert "trader-server is already running" in message
    assert "现有服务地址->http://127.0.0.1:5050" in message
    assert "请在原启动终端按 Ctrl+C 正常停止后，再运行原启动命令（./run.sh 或 .\\run.ps1）" in message
    assert "不要删除 server.lock" in message


def test_budget_uses_its_own_file_and_never_opens_the_legacy_runtime_name() -> None:
    bootstrap = (ROOT / "src/trader/bootstrap.py").read_text(encoding="utf-8")

    assert 'settings.runtime_dir / "deepseek/state.json"' in bootstrap
    assert "BudgetStateFile(budget_path," in bootstrap
    assert 'settings.runtime_dir / "runtime.sqlite3"' not in bootstrap
