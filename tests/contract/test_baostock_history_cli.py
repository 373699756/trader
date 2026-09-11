from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import HistorySyncProgress
from trader.entrypoints.cli import build_parser, main


def test_download_history_is_a_zero_argument_command() -> None:
    args = build_parser().parse_args(["download_history"])

    assert args.command == "download_history"
    assert not hasattr(args, "runtime_dir")
    assert not hasattr(args, "sessions")
    assert not hasattr(args, "mode")


@pytest.mark.parametrize("arguments", (("--runtime-dir", "/tmp/history"), ("--sessions", "2000"), ("--mode", "update")))
def test_download_history_rejects_every_legacy_argument_during_parsing(
    arguments: tuple[str, ...], tmp_path: Path
) -> None:
    runtime_dir = tmp_path / "must-not-exist"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["download_history", *arguments])

    assert not runtime_dir.exists()


def test_download_history_runs_the_typed_zero_argument_synchronization(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = HistoryMaintenanceStatus(
        "completed",
        None,
        Path("data/history/baostock"),
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

    def synchronize(*_args, progress, **_kwargs):
        progress.publish(HistorySyncProgress("supplier_calendar", "waiting", 0, 1, call_elapsed_seconds=5.0))
        return status

    monkeypatch.setattr("trader.infra.research.history_archive_sync.run_history_sync", synchronize)

    assert main(["download_history"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload == {
        "active_snapshot_hash": "a" * 64,
        "archive_root": "data/history/baostock",
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
    }
    progress_lines = captured.err.splitlines()
    assert progress_lines == ["00:00:00 | 同步完成"]
    assert "completed_units" not in captured.err
    assert "total_units" not in captured.err


def test_download_history_contract_exposes_an_immutable_typed_status() -> None:
    status = HistoryMaintenanceStatus(
        "already_current",
        None,
        Path("data/history/baostock"),
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
    assert status.archive_root == Path("data/history/baostock")
    assert status.training_due is False
    assert status.training_due_reason == "data_incomplete"
    with pytest.raises(ValueError, match="flag and reason disagree"):
        replace(status, training_due=True)
    with pytest.raises(AttributeError):
        status.state = "completed"  # type: ignore[misc]


def test_train_tomorrow_uses_the_project_data_roots() -> None:
    from trader.entrypoints.research_commands import _history_data_root, _train_data_root

    root = Path(__file__).resolve().parents[2]

    assert _history_data_root() == root / "data" / "history"
    assert _train_data_root() == root / "data" / "train"


def test_train_tomorrow_is_a_zero_argument_command() -> None:
    args = build_parser().parse_args(["train-tomorrow"])

    assert args.command == "train-tomorrow"
    assert not hasattr(args, "runtime_dir")
    assert not hasattr(args, "allow_partial_history")


def test_train_tomorrow_rejects_an_explicit_history_root_during_parsing(tmp_path: Path) -> None:
    history = tmp_path / "must-not-be-used"

    with pytest.raises(SystemExit):
        build_parser().parse_args(["train-tomorrow", "--runtime-dir", str(history)])

    assert not history.exists()
