"""Public command orchestration for the download business."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncConfiguration, HistorySyncProgressPort
from trader.download.entrypoints.history_maintenance_projection import project_history_maintenance_status
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress


def run_download(repository_root: Path) -> int:
    """Run zero-argument initial or incremental archive maintenance."""

    from trader.bootstrap import execute_history_download

    configuration = history_sync_configuration(repository_root)
    progress = StderrHistorySyncProgress()
    status = execute_history_download(configuration, progress=progress)
    progress.publish_result(status)
    print(json.dumps(project_history_maintenance_status(status), ensure_ascii=False, sort_keys=True))
    return 0 if status.state in {"completed", "already_current"} else 1


def history_sync_configuration(repository_root: Path) -> HistorySyncConfiguration:
    return HistorySyncConfiguration.for_repository(repository_root)


def run_download_command(command: str, *, config_path: Path | None = None) -> int:
    """Run one download-owned public command after root dispatch."""

    repository_root = _repository_root()
    if command == "download":
        return run_download(repository_root)
    if command == "history-automation-status":
        from trader.download.entrypoints.history_automation_projection import project_history_automation_status
        from trader.download.infra.history_automation_status import read_history_automation_status

        automation_status = read_history_automation_status(repository_root / "data/history/baostock", _shanghai_now())
        print(
            json.dumps(
                project_history_automation_status(automation_status),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    if command != "scheduled-history-maintenance" or config_path is None:
        raise ValueError(f"unsupported download command: {command}")
    from trader.bootstrap import execute_history_download
    from trader.download.entrypoints.history_automation_projection import project_history_automation_run_status
    from trader.download.infra.history_maintenance_runner import (
        PlatformHistoryDesktopNotifier,
        RotatingHistoryAutomationLog,
        run_scheduled_history_maintenance,
    )
    from trader.infra.settings import load_runtime_settings

    runtime = load_runtime_settings(config_path)
    observed_at = _shanghai_now()
    configuration = history_sync_configuration(repository_root)
    task_log = RotatingHistoryAutomationLog(runtime.runtime_dir / "logs/history-automation.log")

    def synchronize(progress: HistorySyncProgressPort | None) -> HistoryMaintenanceStatus:
        return execute_history_download(
            configuration,
            supplier_progress=task_log,
            clock=lambda: observed_at,
            progress=progress,
        )

    try:
        run_status = run_scheduled_history_maintenance(
            configuration,
            synchronize,
            PlatformHistoryDesktopNotifier(),
            observed_at,
            progress=task_log,
        )
        try:
            task_log.publish_run(run_status)
        except OSError:
            print(
                '{"reason":"log_write_failed","schema_version":"history_automation_log","state":"degraded"}',
                file=sys.stderr,
                flush=True,
            )
        print(
            json.dumps(
                project_history_automation_run_status(run_status),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if run_status.successful else 1
    finally:
        task_log.close()


def _shanghai_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _repository_root() -> Path:
    current = Path.cwd().resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "src/trader").is_dir():
            return candidate
    return Path("/__trader_source_not_found__")


__all__ = ["history_sync_configuration", "run_download", "run_download_command"]
