from __future__ import annotations

from tests.helpers.shared.configuration import SqlitePolicyFixture

import json
import re
import os
import subprocess
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from trader.download.domain.history_maintenance import (
    HistoryMaintenanceStatus,
)
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.entrypoints.cli import BuildParser, Main
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from tests.helpers.shared.reference_fixtures import PreparedFixture

PROJECT_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("target", ("history", "market", "company"))
def test_fill_is_forwarded_by_shell_and_parser(tmp_path, target):
    parsed = BuildParser().parse_args(["sync", target, "--fill"])
    assert parsed.target == target and parsed.fill is True
    assert BuildParser().parse_args(["sync", target]).fill is False
    binary = tmp_path / "bin"
    binary.mkdir()
    for name in ("python3", "trader-cli"):
        path = binary / name
        path.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        path.chmod(0o755)
    command = subprocess.run(["bash", str(PROJECT_ROOT / "run.sh"), "sync", target, "--fill"], env={**os.environ, "VENV_DIR": str(tmp_path)}, capture_output=True, text=True, timeout=10, check=False)
    assert command.returncode == 0, command.stderr
    assert command.stdout.splitlines()[-3:] == ["sync", target, "--fill"]


@pytest.mark.parametrize("arguments", (["sync", "all", "--fill"], ["sync", "--fill"], ["sync", "company", "--fill", "baostock"]))
def test_fill_rejects_incompatible_commands_before_configuration(arguments, monkeypatch):
    monkeypatch.setattr("trader.entrypoints.cli.LoadRuntimeSettings", lambda *_: pytest.fail("configuration was touched"))
    with pytest.raises(SystemExit) as error:
        Main(arguments)
    assert error.value.code == 2


@pytest.mark.parametrize("option", (("--risk-history",), ("--repair-documents", "ocr"), ("--repair-documents", "limits"), ("--supplement",), ("--repair-source", "baostock")))
def test_retired_fill_options_are_rejected_before_configuration(option, monkeypatch):
    monkeypatch.setattr("trader.entrypoints.cli.LoadRuntimeSettings", lambda *_: pytest.fail("configuration was touched"))
    with pytest.raises(SystemExit) as error:
        Main(["sync", "company", *option])
    assert error.value.code == 2


@pytest.mark.parametrize("target", ("history", "market"))
@pytest.mark.parametrize("fill_missing", (False, True))
def test_fill_selects_existing_price_repair_path_without_changing_normal_sync(tmp_path, monkeypatch, target, fill_missing):
    from trader.bootstrap import ExecuteSync

    settings = replace(LoadRuntimeSettings(PROJECT_ROOT / "config/runtime.json"), data_paths=DataPaths(tmp_path / "data"))
    reference = PreparedFixture(datetime(2026, 11, 1, tzinfo=timezone.utc))
    calls = []

    def download(*args, repair_only, **kwargs):
        calls.append(repair_only)
        if target == "market":
            return QfqUpdateResult(date(2026, 11, 1), 1)
        return HistoryMaintenanceStatus("completed", None, tmp_path, "baostock", None, "a" * 64, date(2026, 11, 1), date(2026, 11, 1))

    monkeypatch.setattr("trader.bootstrap.LoadRuntimeSettings", lambda _: settings)
    monkeypatch.setattr("trader.bootstrap._prepare_reference", lambda *args, **kwargs: reference)
    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", download)
    monkeypatch.setattr("trader.bootstrap.ExecuteQfqDownload", download)
    result = ExecuteSync(PROJECT_ROOT / "config/runtime.json", target, fill_missing=fill_missing)
    assert result.exit_code == 0 and calls == [fill_missing]
    assert not (tmp_path / "data").exists()


def test_sync_rejects_the_retired_old_standard_industry_import(tmp_path):
    with pytest.raises(SystemExit) as failure:
        BuildParser().parse_args(["sync", "market", "--industry-evidence", str(tmp_path / "reviewed.json")])
    assert failure.value.code == 2
    assert not (tmp_path / "reviewed.json").exists()


def test_train_missing_local_history_fails_without_download(tmp_path, monkeypatch, capsys):
    from tests.helpers.shared.cold_start import _empty_config

    config = _empty_config(tmp_path)
    (config.parent / "download.json").write_text("{}", encoding="utf-8")

    def forbidden(*_args, **_kwargs):
        pytest.fail("missing local history must not trigger download or reference preparation")

    for target in ("ExecuteSync", "ExecuteHistoryDownload", "ExecuteQfqDownload", "_prepare_reference"):
        monkeypatch.setattr(f"trader.bootstrap.{target}", forbidden)
    monkeypatch.setattr("requests.Session.request", forbidden)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda *_args: None)

    assert Main(["--config", str(config), "train", "v2", "--json"]) == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    timing = result.pop("timing")
    assert timing["source_read_seconds"] == timing["peak_cpu_reservations"] == timing["peak_memory_reservations"] == 0
    assert timing["profiles"][0]["profile"] == "v2"
    assert timing["profiles"][0]["started_after_seconds"] is None
    assert result["schema_version"] == "selected_training_result"
    assert result["status"] == "failed" and result["failure"] == "current_industry_not_published"
    assert result["diagnostic"]["causes"][0]["error_type"] == "ValueError"
    assert "./run.sh sync history" in captured.err
    assert "正在准备官方总体" not in captured.err
    assert not tuple((tmp_path / "data").rglob("*.sqlite3"))
    assert not tuple((tmp_path / "data").rglob("model.json"))


def test_v1_training_dispatch_preserves_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.helpers.shared.cold_start import _empty_config
    from trader.training.application.learned_rank_training import V1TrainingResult

    config = _empty_config(tmp_path)
    calls = []

    def forbid_other_profiles(*_args, **_kwargs):
        pytest.fail("V1 admission queried unrelated model state")

    def fit_v1(*_args, **_kwargs):
        calls.append("v1")
        return V1TrainingResult("engineering_ready", "fixture", "a" * 64, 0)

    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", forbid_other_profiles)
    monkeypatch.setattr(
        "trader.training.entrypoints.selected_training.EvaluateAllHistoryTrainingDue", forbid_other_profiles
    )

    def selected(runtime, profiles, **_kwargs):
        assert profiles == ("v1",)
        result = fit_v1()
        print(
            json.dumps(
                {
                    "schema_version": "profile_training_result",
                    "profile": "v1",
                    "status": result.status,
                    "production_authority": False,
                }
            )
        )
        return 0

    monkeypatch.setattr("trader.bootstrap.ExecuteSelectedTraining", selected)

    assert Main(["--config", str(config), "train", "v1"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert calls == ["v1"]
    assert payload["profile"] == "v1" and payload["production_authority"] is False
    assert "双档" not in captured.err


@pytest.mark.parametrize("exit_code", (0, 1, 130))
def test_train_preserves_selected_training_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    from tests.helpers.shared.cold_start import _empty_config

    config = _empty_config(tmp_path)
    monkeypatch.setattr(
        "trader.bootstrap.ExecuteSelectedTraining",
        lambda _runtime, _profiles, **_options: exit_code,
    )
    assert Main(["--config", str(config), "train"]) == exit_code
    assert capsys.readouterr().out == ""


def test_download_is_a_zero_argument_command() -> None:
    args = BuildParser().parse_args(["train"])

    assert args.command == "train"
    assert not hasattr(args, "runtime_dir")
    assert not hasattr(args, "sessions")
    assert not hasattr(args, "mode")


def test_download_history_is_removed_instead_of_kept_as_a_compatibility_alias() -> None:
    with pytest.raises(SystemExit):
        BuildParser().parse_args(["download_history"])


@pytest.mark.parametrize("arguments", (("--runtime-dir", "/tmp/history"), ("--sessions", "2000"), ("--mode", "update")))
def test_download_rejects_every_legacy_argument_during_parsing(arguments: tuple[str, ...], tmp_path: Path) -> None:
    runtime_dir = tmp_path / "must-not-exist"

    with pytest.raises(SystemExit):
        BuildParser().parse_args(["train", *arguments])

    assert not runtime_dir.exists()


def test_download_runs_the_typed_zero_argument_synchronization(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = HistoryMaintenanceStatus(
        "completed", None, Path("data/history"), "baostock", None, "a" * 64, date(2026, 9, 10), date(2026, 9, 9)
    )

    def synchronize(_configuration, supplier, progress, **_kwargs):
        from trader.download.infra.history_supplier_router import HistorySupplierRouter

        assert isinstance(supplier, HistorySupplierRouter)
        assert supplier._industry_cache is not None
        assert supplier._industry_cache.path == _configuration.reference_root / "industry.sqlite3"
        progress.publish(HistorySyncProgress("supplier_calendar", "waiting", 0, 1, call_elapsed_seconds=5.0))
        return status

    monkeypatch.setattr("trader.bootstrap.ExecuteHistorySync", synchronize)
    from tests.helpers.shared.reference_fixtures import PreparedFixture
    from tests.helpers.download.history.infra.history_sync import NOW

    monkeypatch.setattr(
        "trader.bootstrap._prepare_reference",
        lambda *_args, **_kwargs: PreparedFixture(NOW, industry_version="fixture"),
    )

    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "trader.bootstrap.ExecuteSelectedTraining",
        lambda _runtime: pytest.fail("incomplete history must block training"),
    )
    from trader.download.entrypoints.commands import RunDownload

    assert RunDownload(PROJECT_ROOT, PROJECT_ROOT / "config/runtime.json", sqlite_policy=SqlitePolicyFixture()) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "snapshot_hash": "a" * 64,
        "history_root": "data/history",
        "data_cutoff": "2026-09-10",
        "efficient_daily_source": None,
        "label_cutoff": "2026-09-09",
        "reason": None,
        "schema_version": "history_maintenance_status",
        "selected_baseline_source": "baostock",
        "state": "completed",
        "unresolved_price_cells": 0,
        "price_gaps": None,
        "price_review": None,
        "industry": None,
    }
    assert "\x1b" not in captured.err and "\x1b" not in captured.out
    progress_lines = re.sub(r" +", " ", captured.err).splitlines()
    assert progress_lines == [
        "00:00:00 | 交易日历 | 等待 | 调用 00:00:05",
        "00:00:00 | 同步完成",
    ]
    assert "completed_units" not in captured.err
    assert "total_units" not in captured.err


def test_train_resource_setup_interrupt_returns_130(tmp_path, monkeypatch, capsys):
    from tests.helpers.shared.cold_start import _empty_config

    config = _empty_config(tmp_path)

    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", interrupt)
    monkeypatch.setattr(
        "trader.training.entrypoints.selected_training.RunSelectedTraining",
        lambda *args, **kwargs: pytest.fail("training ran after resource setup was interrupted"),
    )
    assert Main(["--config", str(config), "train", "--json"]) == 130
    assert json.loads(capsys.readouterr().out) == {
        "schema_version": "selected_training_result",
        "status": "cancelled",
    }


def test_download_contract_exposes_an_immutable_typed_status() -> None:
    status = HistoryMaintenanceStatus(
        "already_current", None, Path("data/history"), "baostock", None, "a" * 64, date(2026, 9, 10), date(2026, 9, 9)
    )

    assert status.state == "already_current"
    assert status.reason is None
    assert status.history_root == Path("data/history")
    assert status.published and not status.needs_retry
    with pytest.raises(ValueError, match="must not be negative"):
        replace(status, unresolved_price_cells=-1)
    with pytest.raises(AttributeError):
        status.state = "completed"  # type: ignore[misc]


def test_training_uses_configured_data_roots():
    from trader.infra.settings import LoadRuntimeSettings

    runtime = LoadRuntimeSettings(PROJECT_ROOT / "config/runtime.json")
    assert runtime.data_paths.history == PROJECT_ROOT / "data/market/history"
    assert runtime.data_paths.models == PROJECT_ROOT / "data/training/models"


def test_train_tomorrow_is_removed_from_the_public_cli() -> None:
    with pytest.raises(SystemExit):
        BuildParser().parse_args(["train-tomorrow"])


def test_download_risk_is_a_zero_argument_command() -> None:
    args = BuildParser().parse_args(["sync", "company"])

    assert (args.command, args.target) == ("sync", "company")
    assert not hasattr(args, "runtime_dir")


def test_download_qfq_is_a_zero_argument_command() -> None:
    args = BuildParser().parse_args(["sync", "market"])

    assert (args.command, args.target) == ("sync", "market")
    assert not hasattr(args, "runtime_dir")


def test_train_is_a_zero_argument_command() -> None:
    args = BuildParser().parse_args(["train"])

    assert args.command == "train"
    assert not hasattr(args, "runtime_dir")


def test_removed_train_tomorrow_rejects_an_explicit_history_root_during_parsing(tmp_path: Path) -> None:
    history = tmp_path / "must-not-be-used"

    with pytest.raises(SystemExit):
        BuildParser().parse_args(["train-tomorrow", "--runtime-dir", str(history)])

    assert not history.exists()
