from __future__ import annotations

from datetime import date
from pathlib import Path

from trader.application.research.history_maintenance import HistoryMaintenanceStatus
from trader.application.research.history_sync import HistorySyncProgress
from trader.entrypoints.history_sync_progress import StderrHistorySyncProgress


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def _failed_status(reason: str) -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus(
        "failed",
        reason,
        Path("data/history/baostock"),
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


def test_supplier_waiting_heartbeat_is_silent_but_retry_is_reported(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 978.061))

    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "waiting",
            0,
            1,
            current_item="2021-01-04",
            attempt=3,
            max_attempts=3,
            call_elapsed_seconds=10.849,
        )
    )
    assert capsys.readouterr().err == ""

    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "retrying",
            0,
            1,
            current_item="2021-01-04",
            attempt=3,
            max_attempts=3,
        )
    )

    assert capsys.readouterr().err == "00:14:38 | 行业快照 | 重试 | 日期 2021-01-04 | 尝试 3/3\n"


def test_daily_download_prints_only_after_one_stock_is_complete(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 135.0))

    progress.publish(HistorySyncProgress("downloading_codes", "started", 12, 100, current_item="600001"))
    progress.publish(
        HistorySyncProgress(
            "supplier_daily_raw",
            "waiting",
            0,
            1,
            current_item="sh.600001",
            attempt=1,
            max_attempts=3,
            call_elapsed_seconds=5.9,
        )
    )
    progress.publish(HistorySyncProgress("supplier_daily_raw", "completed", 1, 1, current_item="sh.600001"))
    progress.publish(HistorySyncProgress("supplier_daily_qfq", "completed", 1, 1, current_item="sh.600001"))
    progress.publish(HistorySyncProgress("downloading_codes", "completed", 13, 100, current_item="600001"))

    assert capsys.readouterr().err == "00:00:35 | 股票下载 | 完成 | 13/100 (13.00%) | 股票 600001\n"


def test_failed_result_keeps_the_specific_supplier_stage_and_error_code(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 145.0, 1070.569))
    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "failed",
            0,
            1,
            current_item="2021-01-04",
            attempt=3,
            max_attempts=3,
            call_elapsed_seconds=45.0,
        )
    )
    progress.publish(HistorySyncProgress("loading_context", "failed", 0, 1))

    progress.publish_result(_failed_status("supplier_industry_timeout"))

    assert capsys.readouterr().err.splitlines()[-1] == (
        "00:16:10 | 同步失败 | 行业快照 | 日期 2021-01-04 | 错误 supplier_industry_timeout"
    )


def test_successful_supplier_retry_clears_the_transient_failure_location(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 110.0, 120.0, 130.0, 140.0))
    progress.publish(
        HistorySyncProgress(
            "supplier_calendar",
            "failed",
            0,
            1,
            current_item="2021-01-01:2026-09-11",
            attempt=1,
            max_attempts=3,
            call_elapsed_seconds=10.0,
        )
    )
    progress.publish(HistorySyncProgress("supplier_industry", "completed", 1, 1, current_item="2026-09-11"))
    progress.publish(HistorySyncProgress("loading_context", "failed", 0, 1))

    progress.publish_result(_failed_status("history_sync_failed"))

    assert capsys.readouterr().err.splitlines()[-1] == ("00:00:40 | 同步失败 | 加载上下文 | 错误 history_sync_failed")


def test_completed_result_keeps_machine_json_out_of_the_progress_stream(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 101.0))
    completed = _failed_status("supplier_industry_timeout")
    completed = HistoryMaintenanceStatus(
        "completed",
        None,
        completed.archive_root,
        completed.selected_baseline_source,
        None,
        "a" * 64,
        date(2026, 9, 10),
        date(2026, 9, 9),
        0,
        False,
        "data_incomplete",
        False,
    )

    progress.publish_result(completed)

    assert capsys.readouterr().err == "00:00:01 | 同步完成\n"
