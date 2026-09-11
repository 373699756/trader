from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.application.research.history_automation import (
    HistoryDesktopNotification,
    HistoryNotificationResult,
)
from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import HistorySyncConfiguration, HistorySyncProgress
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
)
from trader.infra.research.history_control_repository import (
    HistoryMaintenanceLock,
    SQLiteHistoryControlRepository,
)
from trader.infra.research.history_maintenance_runner import (
    PlatformHistoryDesktopNotifier,
    RotatingHistoryAutomationLog,
    run_scheduled_history_maintenance,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 10, 20, 30, tzinfo=SHANGHAI)


class _Notifier:
    def __init__(self, result: HistoryNotificationResult | None = None) -> None:
        self.result = result or HistoryNotificationResult("sent", None)
        self.notifications: list[HistoryDesktopNotification] = []

    def notify(self, notification: HistoryDesktopNotification) -> HistoryNotificationResult:
        self.notifications.append(notification)
        return self.result


def _seed_due_control(root: Path) -> None:
    repository = SQLiteHistoryControlRepository(root / "control.sqlite3")
    repository.initialize()
    source = HistorySourceIdentity("baostock", "baostock_daily", "python-sdk", NOW)
    calendar = HistoryCalendarIdentity((date(2026, 9, 9), date(2026, 9, 10)), source.content_hash)
    universe = HistoryUniverseIdentity(
        (HistorySecurityIdentity("600001", "浦发银行", "main", date(1999, 11, 10), None),),
        source.content_hash,
    )
    snapshot = HistoryActiveSnapshot(
        1,
        NOW.date(),
        NOW.date(),
        calendar.content_hash,
        universe.content_hash,
        source.content_hash,
        (HistorySnapshotPartition("partitions/2026/09.sqlite3", "a" * 64, 2),),
    )
    repository.save_source(source)
    repository.save_calendar(calendar)
    repository.save_universe(universe)
    repository.save_due_state(
        HistoryTrainingDueState("due-20260910", "initial_training_required", None, NOW.date(), 0, False, NOW)
    )
    repository.publish_snapshot(snapshot)


def _completed(root: Path) -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus(
        "completed",
        None,
        root,
        "baostock",
        None,
        "a" * 64,
        NOW.date(),
        NOW.date(),
        0,
        True,
        "initial_training_required",
        False,
    )


def test_scheduled_due_notification_is_atomic_across_repeated_runs_restart_and_dates(tmp_path: Path) -> None:
    archive = tmp_path / "history"
    _seed_due_control(archive)
    configuration = HistorySyncConfiguration(archive_root=archive, training_root=tmp_path / "train")
    notifier = _Notifier()

    def synchronize(_progress):
        return _completed(archive)

    first = run_scheduled_history_maintenance(configuration, synchronize, notifier, NOW)
    repeated = run_scheduled_history_maintenance(configuration, synchronize, notifier, NOW)
    next_day = run_scheduled_history_maintenance(configuration, synchronize, notifier, NOW.replace(day=11))

    assert first.notification_state == "sent"
    assert repeated.notification_state == "sent"
    assert next_day.notification_state == "sent"
    assert len(notifier.notifications) == 3
    assert sum("训练已到期" in item.body for item in notifier.notifications) == 2
    persisted = SQLiteHistoryControlRepository(archive / "control.sqlite3").load_state()
    assert tuple(item.reminder_date for item in persisted.reminder_claims) == (
        date(2026, 9, 10),
        date(2026, 9, 11),
    )


def test_notification_failure_is_recorded_without_failing_a_successful_download(tmp_path: Path) -> None:
    archive = tmp_path / "history"
    _seed_due_control(archive)
    configuration = HistorySyncConfiguration(archive_root=archive, training_root=tmp_path / "train")
    notifier = _Notifier(HistoryNotificationResult("notification_degraded", "notification_service_unavailable"))

    status = run_scheduled_history_maintenance(configuration, lambda _progress: _completed(archive), notifier, NOW)

    assert status.maintenance_state == "completed"
    assert status.notification_state == "notification_degraded"
    assert status.successful is True
    reminder = SQLiteHistoryControlRepository(archive / "control.sqlite3").load_state().reminders[0]
    assert reminder.outcome == "notification_degraded"
    assert reminder.error_code == "notification_service_unavailable"


def test_missing_desktop_notification_service_has_a_stable_degraded_result() -> None:
    notifier = PlatformHistoryDesktopNotifier(platform="linux", executable_resolver=lambda _name: None)

    result = notifier.notify(HistoryDesktopNotification("Trader 历史同步", "历史同步结束。"))

    assert result == HistoryNotificationResult("notification_degraded", "notification_service_unavailable")


def test_scheduled_overlap_returns_already_running_without_notification(tmp_path: Path) -> None:
    archive = tmp_path / "history"
    configuration = HistorySyncConfiguration(archive_root=archive, training_root=tmp_path / "train")
    notifier = _Notifier()
    outer_lock = HistoryMaintenanceLock(archive / ".automation.lock")
    outer_lock.acquire()
    try:
        status = run_scheduled_history_maintenance(
            configuration,
            lambda _progress: _completed(archive),
            notifier,
            NOW,
        )
    finally:
        outer_lock.release()

    assert status.maintenance_state == "already_running"
    assert status.notification_state == "not_attempted"
    assert status.successful is True
    assert notifier.notifications == []


def test_manual_download_overlap_is_not_misreported_as_a_completed_notification(tmp_path: Path) -> None:
    archive = tmp_path / "history"
    configuration = HistorySyncConfiguration(archive_root=archive, training_root=tmp_path / "train")
    notifier = _Notifier()
    overlapping = HistoryMaintenanceStatus(
        "already_running",
        "history_maintenance_running",
        archive,
        "baostock",
        None,
        None,
        None,
        None,
        0,
        False,
        "data_incomplete",
        False,
    )

    status = run_scheduled_history_maintenance(configuration, lambda _progress: overlapping, notifier, NOW)

    assert status.maintenance_state == "already_running"
    assert status.notification_state == "not_attempted"
    assert notifier.notifications == []


def test_task_log_rotates_and_omits_stock_detail(tmp_path: Path) -> None:
    log_path = tmp_path / "history-automation.log"
    task_log = RotatingHistoryAutomationLog(log_path, max_bytes=300, backup_count=2)
    for index in range(12):
        task_log.publish(
            HistorySyncProgress(
                "downloading_codes",
                "waiting",
                index,
                12,
                current_item="600001",
                call_elapsed_seconds=float(index),
            )
        )
    task_log.close()

    logs = tuple(path for path in sorted(tmp_path.glob("history-automation.log*")) if path.suffix != ".lock")
    assert 2 <= len(logs) <= 3
    combined = "".join(path.read_text(encoding="utf-8") for path in logs)
    assert "600001" not in combined
    assert "current_item" not in combined
    assert all(json.loads(line)["schema_version"] == "history_automation_log" for line in combined.splitlines())
