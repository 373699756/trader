from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

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


def test_download_history_fails_closed_until_the_new_control_plane_exists(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["download_history"]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "active_snapshot_hash": None,
        "archive_root": "data/history/baostock",
        "automatic_training": False,
        "data_cutoff": None,
        "efficient_daily_source": None,
        "label_cutoff": None,
        "matured_label_days_since_training": 0,
        "reason": "history_control_plane_pending",
        "schema_version": "history_maintenance_status",
        "selected_baseline_source": "baostock",
        "state": "blocked",
        "training_due": False,
        "training_due_reason": "data_incomplete",
    }


def test_download_history_contract_exposes_an_immutable_typed_status() -> None:
    from trader.application.research.history_maintenance import blocked_history_maintenance_status

    status = blocked_history_maintenance_status()

    assert status.state == "blocked"
    assert status.reason == "history_control_plane_pending"
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


def test_train_tomorrow_can_reuse_an_explicit_download_history_root() -> None:
    args = build_parser().parse_args(["train-tomorrow", "--runtime-dir", "/tmp/trader-baostock"])

    assert args.command == "train-tomorrow"
    assert args.runtime_dir == Path("/tmp/trader-baostock")
    assert not hasattr(args, "allow_partial_history")
