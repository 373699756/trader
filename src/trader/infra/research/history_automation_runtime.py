"""Unattended history synchronization, reminder, notification, and task logging."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol
from zoneinfo import ZoneInfo

from trader.application.research.history_automation import (
    HistoryAutomationRunStatus,
    HistoryAutomationStatus,
    HistoryDesktopNotification,
    HistoryDesktopNotifier,
    HistoryNotificationResult,
    HistoryNotificationState,
)
from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import (
    HistorySyncConfiguration,
    HistorySyncProgress,
    HistorySyncProgressPort,
)
from trader.domain.research.history_control import HistoryReminderClaim, HistoryReminderState
from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.research.history_automation_status import read_history_automation_status
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    SQLiteHistoryControlRepository,
)

ScheduledSynchronizer = Callable[[HistorySyncProgressPort | None], HistoryMaintenanceStatus]
PlatformName = Literal["linux", "windows", "macos"]
ExecutableResolver = Callable[[str], str | None]

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class CompletedCommand(Protocol):
    @property
    def returncode(self) -> int: ...


CommandRunner = Callable[..., CompletedCommand]


class RotatingHistoryAutomationLog:
    """Final observability adapter with bounded, stock-detail-free JSON lines."""

    def __init__(self, path: Path, *, max_bytes: int = 1024 * 1024, backup_count: int = 5) -> None:
        if max_bytes < 1 or backup_count < 1:
            raise ValueError("history automation log rotation is invalid")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._lock_path = Path(f"{path}.lock")
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._started_at = time.monotonic()

    def publish(self, progress: HistorySyncProgress) -> None:
        percent = 100.0 if progress.total_units == 0 else progress.completed_units / progress.total_units * 100.0
        self._write(
            {
                "schema_version": "history_automation_log",
                "event": "sync_progress",
                "stage": progress.stage,
                "state": progress.state,
                "completed_units": progress.completed_units,
                "total_units": progress.total_units,
                "percent": round(percent, 2),
                "attempt": progress.attempt,
                "max_attempts": progress.max_attempts,
                "call_elapsed_seconds": round(progress.call_elapsed_seconds, 3),
                "elapsed_seconds": round(time.monotonic() - self._started_at, 3),
            }
        )

    def publish_run(self, status: HistoryAutomationRunStatus) -> None:
        self._write(
            {
                "schema_version": "history_automation_log",
                "event": "run_completed",
                "observed_at": status.observed_at.isoformat(),
                "maintenance_state": status.maintenance_state,
                "maintenance_reason": status.maintenance_reason,
                "training_due": status.due_status.training_due,
                "training_due_reason": status.due_status.training_due_reason,
                "matured_label_days_since_training": status.due_status.matured_label_days_since_training,
                "notification_state": status.notification_state,
                "notification_error_code": status.notification_error_code,
                "automatic_model_update": status.automatic_model_update,
            }
        )

    def close(self) -> None:
        return

    def _write(self, payload: dict[str, object]) -> None:
        line = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
        lock = self._acquire_log_lock()
        try:
            self._rotate(len(line))
            with self._path.open("ab") as handle:
                handle.write(line)
                handle.flush()
        finally:
            lock.release()

    def _acquire_log_lock(self) -> ProcessLock:
        deadline = time.monotonic() + 2.0
        while True:
            lock = ProcessLock(self._lock_path)
            try:
                lock.acquire()
                return lock
            except ProcessLockError as exc:
                if time.monotonic() >= deadline:
                    raise OSError("history automation log is busy") from exc
                time.sleep(0.01)

    def _rotate(self, incoming_bytes: int) -> None:
        if not self._path.exists() or self._path.stat().st_size + incoming_bytes <= self._max_bytes:
            return
        Path(f"{self._path}.{self._backup_count}").unlink(missing_ok=True)
        for index in range(self._backup_count - 1, 0, -1):
            source = Path(f"{self._path}.{index}")
            if source.exists():
                source.replace(Path(f"{self._path}.{index + 1}"))
        self._path.replace(Path(f"{self._path}.1"))


class PlatformHistoryDesktopNotifier:
    def __init__(
        self,
        *,
        platform: PlatformName | None = None,
        executable_resolver: ExecutableResolver = shutil.which,
        command_runner: CommandRunner = subprocess.run,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0.0:
            raise ValueError("history notification timeout must be positive")
        self._platform = platform or _current_platform()
        self._resolve = executable_resolver
        self._run = command_runner
        self._timeout = timeout_seconds

    def notify(self, notification: HistoryDesktopNotification) -> HistoryNotificationResult:
        executable_name, arguments = _notification_command(self._platform, notification)
        executable = self._resolve(executable_name)
        if executable is None:
            return HistoryNotificationResult("notification_degraded", "notification_service_unavailable")
        try:
            completed = self._run(
                (executable, *arguments),
                check=False,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            return HistoryNotificationResult("notification_degraded", "notification_timeout")
        except OSError:
            return HistoryNotificationResult("notification_degraded", "notification_failed")
        if completed.returncode != 0:
            return HistoryNotificationResult("notification_degraded", "notification_failed")
        return HistoryNotificationResult("sent", None)


def run_scheduled_history_maintenance(
    configuration: HistorySyncConfiguration,
    synchronize: ScheduledSynchronizer,
    notifier: HistoryDesktopNotifier,
    observed_at: datetime,
    *,
    progress: HistorySyncProgressPort | None = None,
) -> HistoryAutomationRunStatus:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("history automation clock must be timezone-aware")
    observed_at = observed_at.astimezone(_SHANGHAI)
    archive_root = configuration.archive_root
    try:
        with HistoryMaintenanceLock(archive_root / ".automation.lock"):
            maintenance = synchronize(progress)
            due_status = read_history_automation_status(archive_root, observed_at)
            notification_state, notification_error = _notify_once(
                archive_root,
                maintenance,
                due_status,
                notifier,
                observed_at,
            )
            due_status = read_history_automation_status(archive_root, observed_at)
            result = HistoryAutomationRunStatus(
                observed_at,
                archive_root,
                maintenance.state,
                maintenance.reason,
                due_status,
                notification_state,
                notification_error,
                False,
            )
    except HistoryMaintenanceAlreadyRunningError:
        due_status = read_history_automation_status(archive_root, observed_at)
        result = HistoryAutomationRunStatus(
            observed_at,
            archive_root,
            "already_running",
            "history_automation_running",
            due_status,
            "not_attempted",
            None,
            False,
        )
    return result


def _notify_once(
    archive_root: Path,
    maintenance: HistoryMaintenanceStatus,
    due_status: HistoryAutomationStatus,
    notifier: HistoryDesktopNotifier,
    observed_at: datetime,
) -> tuple[HistoryNotificationState, str | None]:
    if maintenance.state == "already_running":
        return "not_attempted", None
    repository = SQLiteHistoryControlRepository(archive_root / "control.sqlite3")
    due_identity = due_status.due_identity
    if due_status.training_due and due_identity is not None:
        claim = HistoryReminderClaim(due_identity, observed_at.date(), observed_at)
        try:
            claimed = repository.claim_reminder(claim)
        except HistoryControlError:
            return "notification_degraded", "reminder_claim_failed"
        if not claimed:
            result = notifier.notify(_completion_notification(maintenance))
            return result.state, result.error_code
        notification = _due_notification(maintenance, due_status)
        result = notifier.notify(notification)
        reminder = HistoryReminderState(
            due_identity,
            observed_at.date(),
            result.state,
            observed_at,
            result.error_code,
        )
        try:
            repository.save_reminder(reminder)
        except HistoryControlError:
            return "notification_degraded", "reminder_result_persist_failed"
        return result.state, result.error_code
    result = notifier.notify(_completion_notification(maintenance))
    return result.state, result.error_code


def _due_notification(
    maintenance: HistoryMaintenanceStatus,
    due_status: HistoryAutomationStatus,
) -> HistoryDesktopNotification:
    body = (
        f"历史同步：{maintenance.state}；Tomorrow 训练已到期：{due_status.training_due_reason}；"
        f"成熟标签日：{due_status.matured_label_days_since_training}。请手工运行 train-tomorrow。"
    )
    return HistoryDesktopNotification("Trader 历史同步与训练提醒", body)


def _completion_notification(maintenance: HistoryMaintenanceStatus) -> HistoryDesktopNotification:
    reason = f"（{maintenance.reason}）" if maintenance.reason is not None else ""
    return HistoryDesktopNotification("Trader 历史同步", f"历史同步结束：{maintenance.state}{reason}。")


def _current_platform() -> PlatformName:
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    raise RuntimeError("history desktop notification platform is unsupported")


def _notification_command(
    platform: PlatformName,
    notification: HistoryDesktopNotification,
) -> tuple[str, tuple[str, ...]]:
    if platform == "linux":
        return "notify-send", ("--app-name", "Trader", notification.title, notification.body)
    if platform == "macos":
        script = (
            "display notification " + json.dumps(notification.body) + " with title " + json.dumps(notification.title)
        )
        return "osascript", ("-e", script)
    script = (
        "$title=$args[0];$body=$args[1];"
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Information;$n.Visible=$true;"
        "$n.ShowBalloonTip(5000,$title,$body,[System.Windows.Forms.ToolTipIcon]::Info);"
        "Start-Sleep -Milliseconds 500;$n.Dispose()"
    )
    return "powershell.exe", (
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
        notification.title,
        notification.body,
    )


__all__ = [
    "PlatformHistoryDesktopNotifier",
    "RotatingHistoryAutomationLog",
    "run_scheduled_history_maintenance",
]
