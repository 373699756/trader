from __future__ import annotations

from datetime import date
from dataclasses import replace
from pathlib import Path

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import (
    HistoryRecoverySummary,
    HistoryStEligibilitySummary,
    HistorySyncProgress,
    HistoryTencentSummary,
)
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_recovery_attempt_summary_and_incomplete_publication_are_explicit(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    progress.publish(
        HistorySyncProgress(
            "baostock_gap_fill",
            "retrying",
            0,
            45,
            "600001",
            attempt=2,
            max_attempts=3,
            supplier_source="baostock",
            requested_sessions=2000,
        )
    )
    progress.publish(
        HistorySyncProgress(
            "baostock_gap_fill",
            "completed",
            45,
            45,
            recovery_summary=HistoryRecoverySummary(44, 1, 0),
        )
    )
    progress.publish_result(
        replace(_failed_status("fixture"), state="completed", reason=None, unresolved_price_cells=2000)
    )
    lines = capsys.readouterr().err.splitlines()
    assert "0/45" in lines[0] and "尝试 2/3" in lines[0]
    assert "缺口股 45 | 成功 44 股 | 补缺失败 1 股 | 未处理完 0 股" in lines[1]
    assert "同步完成 | 数据不完整 | 未修复价格缺口 2000 股日" in lines[2]


def test_legacy_resume_progress_is_visible_with_counts_and_month(capsys):
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    reporter.publish(HistorySyncProgress("history_gap_resume", "waiting", 64, 65, current_item="2026-09"))
    reporter.publish(HistorySyncProgress("history_gap_resume", "completed", 65, 65, current_item="2026-09"))
    output = capsys.readouterr().err
    assert "旧断点恢复 | 等待 | 64/65" in output
    assert "旧断点恢复 | 完成 | 65/65" in output
    assert "处理项 2026-09" in output


def test_complete_code_reuse_is_not_reported_as_another_network_request(capsys):
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    reporter.publish(HistorySyncProgress("tencent_history", "completed", 1, 2, "600001"))
    output = capsys.readouterr().err
    assert "无新增或缺口，复用" in output
    assert "请求" not in output and "来源 Tencent" not in output


def test_st_checkpoints_report_progress_before_completion(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0.0, 1.0, 2.0))
    progress.publish(HistorySyncProgress("history_st_reference", "completed", 2, 4))
    progress.publish(
        HistorySyncProgress(
            "history_st_reference",
            "completed",
            4,
            4,
            st_summary=HistoryStEligibilitySummary(3, 1, 0),
        )
    )
    output = capsys.readouterr().err.splitlines()
    assert "检查中" in output[0] and "2/4" in output[0]
    assert "完成" in output[1] and "4/4" in output[1]
    assert "合格 3 | 曾ST排除 1 | 未知排除 0" in output[1]


def _failed_status(reason: str) -> HistoryMaintenanceStatus:
    return HistoryMaintenanceStatus(
        "failed",
        reason,
        Path("data/history"),
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


def test_supplier_preparation_waiting_and_retry_are_reported(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 978.061, 978.061))

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
    assert "行业快照 | 等待 | 0/1" in capsys.readouterr().err

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

    assert capsys.readouterr().err == "00:14:38 | 行业快照 | 重试 | 0/1 (0.00%) | 日期 2021-01-04 | 尝试 3/3\n"


def test_industry_progress_reports_date_counts_and_cache_without_duplicate_starts(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0, 1, 1, 1, 2, 3))
    started = HistorySyncProgress("supplier_industry", "started", 4, 18, "2020-07-01")
    progress.publish(started)
    progress.publish(started)
    progress.publish(HistorySyncProgress("supplier_industry", "waiting", 4, 18, "2020-07-01", call_elapsed_seconds=1))
    progress.publish(HistorySyncProgress("supplier_industry", "waiting", 4, 18, "2020-07-01", call_elapsed_seconds=5))
    progress.publish(HistorySyncProgress("supplier_industry", "completed", 5, 18, "2020-07-01", cache_hit=True))
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    assert "开始 | 4/18" in lines[0]
    assert "等待 | 4/18" in lines[1]
    assert "完成 | 5/18" in lines[2] and "缓存复用" in lines[2]


def test_daily_download_prints_only_after_one_stock_is_complete(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 100.0, 106.0, 110.0, 120.0, 135.0))

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

    assert capsys.readouterr().err == "00:00:35 | 股票下载 | 完成 | 13/100 (13.00%) | 股票 600001 | 阶段耗时 00:00:35\n"


def test_partition_sealing_prints_the_first_month_before_long_local_work(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 101.0))

    progress.publish(HistorySyncProgress("sealing_partitions", "started", 0, 100, current_item="2018-07"))

    assert (
        capsys.readouterr().err == "00:00:01 | 封存月分片 | 开始 | 0/100 (0.00%) | 月份 2018-07 | 本月耗时 00:00:00\n"
    )


def test_partition_persistence_reports_start_heartbeat_and_completion(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0.0, 0.0, 1.0, 6.0, 7.0))

    progress.publish(HistorySyncProgress("persisting_partitions", "started", 0, 3, "2026-07"))
    progress.publish(HistorySyncProgress("persisting_partitions", "completed", 1, 3, "2026-07"))
    progress.publish(HistorySyncProgress("persisting_partitions", "started", 1, 3, "2026-08"))
    progress.publish(HistorySyncProgress("persisting_partitions", "completed", 3, 3, "2026-09"))

    assert capsys.readouterr().err.splitlines() == [
        "00:00:00 | 历史月分片写入 | 开始 | 0/3 (0.00%) | 月份 2026-07 | 本月耗时 00:00:00",
        "00:00:06 | 历史月分片写入 | 开始 | 1/3 (33.33%) | 月份 2026-08 | 本月耗时 00:00:00",
        "00:00:07 | 历史月分片写入 | 完成 | 3/3 (100.00%) | 月份 2026-09 | 本月耗时 00:00:00",
    ]


def test_tencent_failure_and_terminal_summary_are_distinct(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=lambda: 0.0)

    progress.publish(
        HistorySyncProgress(
            "tencent_history",
            "failed",
            29,
            4400,
            "600001",
            supplier_source="tencent",
            requested_sessions=2000,
            failure_reason="history_tencent_overlap_conflict",
        )
    )
    progress.publish(
        HistorySyncProgress(
            "tencent_history",
            "completed",
            4400,
            4400,
            tencent_summary=HistoryTencentSummary(4400, 4371, 29, 24),
        )
    )

    assert capsys.readouterr().err.splitlines() == [
        "00:00:00 | Tencent历史下载 | 失败 | 29/4400 (0.66%) | 股票 600001 | 来源 Tencent | 请求 2000 日 "
        "| 错误 history_tencent_overlap_conflict | 阶段耗时 00:00:00",
        "00:00:00 | Tencent历史下载 | 完成 | 4400/4400 (100.00%) | 成功 4371 股 | 失败 29 股 "
        "| 重叠冲突 24 股 | 其它失败 5 股 | 阶段耗时 00:00:00",
    ]


def test_failed_result_keeps_the_specific_supplier_stage_and_error_code(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 145.0, 145.0, 1070.569))
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
