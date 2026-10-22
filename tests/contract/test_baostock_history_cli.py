from __future__ import annotations

import json
import re
import signal
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Literal

import pytest

from trader.download.domain.history_maintenance import (
    HistoryMaintenanceStatus,
    HistoryPriceGapReason,
    HistoryPriceGapSummary,
)
from trader.download.domain.history_sync import HistorySyncProgress
from trader.entrypoints.cli import BuildParser, Main

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(("state", "exit_code"), (("cancelled", 130), ("failed", 1)))
def test_train_preserves_history_cancellation_and_failure(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, state, exit_code
) -> None:
    status = HistoryMaintenanceStatus(
        state,
        state,
        Path("data/market/history"),
        "tencent",
        None,
        None,
        None,
        None,
        0,
        False,
        "data_incomplete",
        False,
    )
    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", lambda *_args, **_kwargs: status)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda: None)
    monkeypatch.setattr(
        "trader.training.entrypoints.commands.RunJointTraining",
        lambda _runtime: pytest.fail("cancelled or failed history must not start training"),
    )
    assert Main(["--config", str(PROJECT_ROOT / "config/runtime.json"), "train"]) == exit_code
    captured = capsys.readouterr()
    assert json.loads(captured.out)["state"] == state
    if state == "cancelled":
        assert "未通过训练准入" not in captured.err


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
        "completed",
        None,
        Path("data/history"),
        "baostock",
        None,
        "a" * 64,
        date(2026, 9, 10),
        date(2026, 9, 9),
        0,
        False,
        "data_incomplete",
        False,
    )

    def synchronize(_configuration, supplier, progress, **_kwargs):
        from trader.download.infra.history_supplier_router import HistorySupplierRouter

        assert isinstance(supplier, HistorySupplierRouter)
        assert supplier._industry_cache is not None
        assert supplier._industry_cache.path == _configuration.reference_root / "industry.sqlite3"
        progress.publish(HistorySyncProgress("supplier_calendar", "waiting", 0, 1, call_elapsed_seconds=5.0))
        return status

    monkeypatch.setattr("trader.download.infra.history_sync_runner.RunHistorySync", synchronize)
    from tests.reference_fixtures import PreparedFixture
    from tests.unit.infra.research.test_history_sync import NOW

    monkeypatch.setattr(
        "trader.bootstrap._prepare_reference",
        lambda *_args, **_kwargs: PreparedFixture(NOW, industry_version="fixture"),
    )

    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda: None)
    monkeypatch.setattr(
        "trader.training.entrypoints.commands.RunJointTraining",
        lambda _runtime: pytest.fail("incomplete history must block training"),
    )
    assert Main(["--config", str(PROJECT_ROOT / "config" / "runtime.json"), "train"]) == 1

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "active_snapshot_hash": "a" * 64,
        "archive_root": "data/history",
        "automatic_training": False,
        "data_cutoff": "2026-09-10",
        "efficient_daily_source": None,
        "label_cutoff": "2026-09-09",
        "matured_label_days_since_training": 0,
        "reason": None,
        "schema_version": "history_maintenance_status",
        "selected_baseline_source": "baostock",
        "state": "completed",
        "training_due": False,
        "training_due_reason": "data_incomplete",
        "training_heads": [],
        "unresolved_price_cells": 0,
        "price_gaps": None,
    }
    assert "\x1b" not in captured.err and "\x1b" not in captured.out
    progress_lines = re.sub(r" +", " ", captured.err).splitlines()
    assert progress_lines == [
        "train: 正在准备官方总体、ST资格、交易日历及当前行业。",
        "00:00:00 | 交易日历 | 等待 | 调用 00:00:05",
        "00:00:00 | 同步完成",
        "完整历史未通过训练准入；未启动双档训练。",
    ]
    assert "completed_units" not in captured.err
    assert "total_units" not in captured.err


def test_train_download_honors_sigterm_and_restores_handlers_before_fitting(monkeypatch, capsys):
    from tests.unit.application.test_sync_data import _history

    previous = signal.getsignal(signal.SIGTERM)

    def download(configuration, *, progress, cancel_requested):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert cancel_requested()
        return _history()

    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", download)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda: None)
    monkeypatch.setattr(
        "trader.training.entrypoints.commands.RunJointTraining", lambda *args, **kwargs: pytest.fail("training ran")
    )
    assert Main(["--config", str(PROJECT_ROOT / "config/runtime.json"), "train"]) == 130
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads(capsys.readouterr().out)["state"] == "cancelled"


def test_train_cancellation_during_reference_preparation_returns_130(monkeypatch, capsys):
    previous = signal.getsignal(signal.SIGTERM)

    def prepare(configuration, *, progress, cancel_requested):
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)
        assert cancel_requested()
        raise RuntimeError("reference_preparation_cancelled")

    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", prepare)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda: None)
    monkeypatch.setattr(
        "trader.training.entrypoints.commands.RunJointTraining", lambda *args, **kwargs: pytest.fail("training ran")
    )
    assert Main(["--config", str(PROJECT_ROOT / "config/runtime.json"), "train"]) == 130
    assert signal.getsignal(signal.SIGTERM) == previous
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"


@pytest.mark.parametrize(
    ("state", "due_reason", "gap_reason", "starts_training"),
    (
        ("completed", "initial_training_required", "tencent_qfq_invalid_price", True),
        ("already_current", "initial_training_required", "history_price_pending", True),
        ("completed", "data_incomplete", "tencent_qfq_invalid_price", False),
        ("failed", "initial_training_required", "tencent_qfq_invalid_price", False),
    ),
)
def test_train_uses_valid_samples_despite_reported_price_gaps(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    state: Literal["completed", "already_current", "failed"],
    due_reason: Literal["initial_training_required", "data_incomplete"],
    gap_reason: str,
    starts_training: bool,
) -> None:
    status = HistoryMaintenanceStatus(
        state,
        "history_price_gaps_remaining" if state != "failed" else "fixture_failure",
        Path("data/history"),
        "tencent_with_baostock_reference",
        "tencent",
        "a" * 64,
        date(2026, 10, 16),
        date(2026, 10, 15),
        0,
        due_reason == "initial_training_required",
        due_reason,
        False,
        3267,
        HistoryPriceGapSummary(
            25,
            3267,
            date(2018, 7, 17),
            date(2022, 4, 27),
            (HistoryPriceGapReason(gap_reason, 3267),),
        ),
    )
    calls: list[object] = []
    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", lambda *_args, **_kwargs: status)
    monkeypatch.setattr("trader.training.entrypoints.commands.ConfigureTrainingResources", lambda: None)
    monkeypatch.setattr(
        "trader.training.entrypoints.commands.RunJointTraining",
        lambda runtime, **kwargs: calls.append(runtime) or 0,
    )

    result = Main(["--config", str(PROJECT_ROOT / "config" / "runtime.json"), "train"])

    assert result == (0 if starts_training else 1)
    assert len(calls) == int(starts_training)
    captured = capsys.readouterr()
    if starts_training:
        assert captured.out == ""  # The stubbed trainer emits nothing; successful history is stderr-only.
    else:
        assert json.loads(captured.out)["price_gaps"]["cells"] == 3267
    assert ("价格缺口待补" in captured.err) == (state != "failed")
    if starts_training:
        assert "按有效样本尝试双档训练" in captured.err
        assert "未启动双档训练" not in captured.err
    else:
        assert "未启动双档训练" in captured.err


def test_download_contract_exposes_an_immutable_typed_status() -> None:
    status = HistoryMaintenanceStatus(
        "already_current",
        None,
        Path("data/history"),
        "baostock",
        None,
        "a" * 64,
        date(2026, 9, 10),
        date(2026, 9, 9),
        0,
        False,
        "data_incomplete",
        False,
    )

    assert status.state == "already_current"
    assert status.reason is None
    assert status.archive_root == Path("data/history")
    assert status.training_due is False
    assert status.training_due_reason == "data_incomplete"
    with pytest.raises(ValueError, match="flag and reason disagree"):
        replace(status, training_due=True)
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
