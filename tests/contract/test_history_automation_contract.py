from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.history_automation import HistoryAutomationStatus
from trader.entrypoints.cli import build_parser, main
from trader.entrypoints.history_automation_projection import project_history_automation_status
from trader.infra.research.history_automation_installation import (
    HistoryAutomationInstallationRequest,
    plan_history_automation_installation,
)


def test_automation_cli_commands_are_zero_argument_and_check_contains_read_only_status() -> None:
    parser = build_parser()

    for command in (
        "scheduled-history-maintenance",
        "install-history-automation",
        "uninstall-history-automation",
        "history-automation-status",
    ):
        assert parser.parse_args([command]).command == command
        with pytest.raises(SystemExit):
            parser.parse_args([command, "unexpected"])

    source = Path("src/trader/entrypoints/cli.py").read_text(encoding="utf-8")
    assert '"history-automation-status"' in source
    assert "evaluate_history_training_due" not in Path(
        "src/trader/infra/research/history_automation_status.py"
    ).read_text(encoding="utf-8")


def test_check_status_projection_is_explicit_and_keeps_automatic_training_disabled() -> None:
    status = HistoryAutomationStatus(
        "ready",
        None,
        Path("data/history/baostock"),
        "a" * 64,
        date(2026, 9, 10),
        date(2026, 9, 10),
        "due-20260910",
        20,
        True,
        "cadence_due",
        date(2026, 9, 11),
        "pending",
        None,
        False,
    )

    assert project_history_automation_status(status) == {
        "schema_version": "history_automation_status",
        "state": "ready",
        "reason": None,
        "archive_root": "data/history/baostock",
        "active_snapshot_hash": "a" * 64,
        "data_cutoff": "2026-09-10",
        "label_cutoff": "2026-09-10",
        "due_identity": "due-20260910",
        "matured_label_days_since_training": 20,
        "training_due": True,
        "training_due_reason": "cadence_due",
        "reminder_date": "2026-09-11",
        "reminder_state": "pending",
        "reminder_error_code": None,
        "automatic_model_update": False,
    }


def test_internal_status_command_reads_persisted_projection_without_loading_supplier(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime(2026, 9, 11, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai"))
    status = HistoryAutomationStatus(
        "data_incomplete",
        "history_control_unavailable",
        Path("data/history/baostock"),
        None,
        None,
        None,
        None,
        0,
        False,
        "data_incomplete",
        now.date(),
        "not_due",
        None,
        False,
    )
    monkeypatch.setattr(
        "trader.infra.research.history_automation_status.read_history_automation_status", lambda *_: status
    )
    monkeypatch.setattr("trader.entrypoints.cli._shanghai_now", lambda: now)

    assert main(["history-automation-status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "data_incomplete"
    assert payload["matured_label_days_since_training"] == 0
    assert payload["automatic_model_update"] is False


def test_linux_user_units_pass_systemd_native_verification_when_available(tmp_path: Path) -> None:
    verifier = shutil.which("systemd-analyze")
    if verifier is None:
        pytest.skip("systemd-analyze is unavailable")
    plan = plan_history_automation_installation(
        HistoryAutomationInstallationRequest(
            "linux",
            Path.cwd().resolve(),
            (Path.cwd() / "config" / "runtime.json").resolve(),
            Path(sys.executable).resolve(),
            tmp_path,
            1000,
        )
    )
    rendered: list[Path] = []
    for managed in plan.files:
        path = tmp_path / managed.path.name
        path.write_text(managed.content, encoding="utf-8")
        rendered.append(path)

    completed = subprocess.run(
        (verifier, "verify", *(str(path) for path in rendered)),
        text=True,
        capture_output=True,
        check=False,
    )

    if completed.returncode != 0 and "Operation not permitted" in completed.stderr:
        pytest.skip("systemd native verification is blocked by the sandbox")
    assert completed.returncode == 0, completed.stderr


def test_linux_timer_calendar_expressions_are_native_shanghai_schedules() -> None:
    verifier = shutil.which("systemd-analyze")
    if verifier is None:
        pytest.skip("systemd-analyze is unavailable")

    completed = subprocess.run(
        (
            verifier,
            "calendar",
            "*-*-* 15:10:00 Asia/Shanghai",
            "*-*-* 20:30:00 Asia/Shanghai",
        ),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "15:10:00 Asia/Shanghai" in completed.stdout
    assert "20:30:00 Asia/Shanghai" in completed.stdout
