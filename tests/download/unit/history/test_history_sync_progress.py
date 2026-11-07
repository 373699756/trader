from __future__ import annotations

import re
from threading import Event, enumerate as enumerate_threads

from dataclasses import replace

from datetime import date



import pytest

from trader.download.application.progress_message import ProgressField, ProgressLevel, ProgressMessage
from trader.download.application.sync_progress import SyncProgress
from trader.infra.progress_format import DisplayWidth

from trader.download.application.qfq_download_progress import QfqDownloadProgress

from trader.download.application.stock_download_progress import FormatStockDownloadProgress

from trader.download.domain.baostock_daily import BaoStockSecurity

from trader.download.domain.history_maintenance import HistoryMaintenanceStatus

from trader.download.domain.history_price_coverage import HistoryPriceCoverage

from trader.download.domain.history_sync import (
    HistoryIndustryReadiness,
    HistoryPipelineProgress,
    HistoryStEligibilitySummary,
    HistorySyncProgress,
    HistoryTencentSummary,
)

from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.application.progress_message import WithProgressTime

from trader.download.infra.terminal_progress import RenderProgress, TerminalDownloadProgress

from tests.helpers.download.history.history_sync_progress import (
    _Clock,
    _Terminal,
    _compact_log,
    _failed_status,
    _stock_progress,
)


def test_local_history_preparation_reports_counts_and_elapsed_before_stocks(capsys) -> None:
    reporter = StderrHistorySyncProgress(monotonic=_Clock(0, 50, 55, 60, 61, 61))
    reporter.publish(HistorySyncProgress("initializing", "started", 0, 4402, "reading_tasks"))
    reporter.publish(HistorySyncProgress("initializing", "waiting", 512, 4402, "reading_tasks"))
    reporter.publish(HistorySyncProgress("initializing", "completed", 4402, 4402, "reading_tasks"))
    reporter.publish(HistorySyncProgress("tencent_history", "started", 0, 4402))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "history·读取历史下载任务 00:00:00 | 开始 | 股票 0/4402" in lines[0]
    assert "读取历史下载任务 00:00:05 | 等待 | 股票 512/4402" in lines[1]
    assert "读取历史下载任务 00:00:10 | 完成 | 股票 4402/4402" in lines[2]
    assert "history·Tencent下载 00:00:00 | 已确认 0/4402 | 开始" in lines[3]


@pytest.mark.parametrize("fail", [False, True])
def test_preparation_heartbeat_keeps_real_counts_and_stops_on_exit(capsys, monkeypatch, fail: bool) -> None:
    clock = [0.0]
    reporter = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    waiting = Event()
    output = reporter._output

    def report(message: ProgressMessage) -> None:
        output(message)
        if "等待" in RenderProgress(message, color=False):
            waiting.set()

    monkeypatch.setattr(reporter, "_output", report)

    def run() -> None:
        with output.keep_alive():
            reporter.publish(HistorySyncProgress("initializing", "started", 512, 4402, "reading_tasks"))
            clock[0] = 5
            output.heartbeat()
            if fail:
                raise RuntimeError("fixture_preparation_failed")
            reporter.publish(HistorySyncProgress("initializing", "completed", 4402, 4402, "reading_tasks"))

    if fail:
        with pytest.raises(RuntimeError, match="fixture_preparation_failed"):
            run()
    else:
        run()
    assert not any(thread.name == "download-progress" for thread in enumerate_threads())
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert any("等待 | 股票 512/4402" in line for line in lines)
    if not fail:
        assert "完成 | 股票 4402/4402" in lines[-1]


def test_industry_retry_and_incomplete_publication_are_explicit(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "retrying",
            0,
            45,
            "301548",
            attempt=2,
            max_attempts=3,
        )
    )
    progress.publish_result(
        replace(_failed_status("fixture"), state="completed", reason="history_price_gaps_remaining",
                snapshot_hash="a" * 64, unresolved_price_cells=2000)
    )
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "0/45" in lines[0] and "尝试 2/3" in lines[0]
    assert "等待 | 历史已发布 | 价格缺口待补 | 未修复价格缺口 2000 股日" in lines[1]


@pytest.mark.parametrize("failure", (None, "supplier_industry_timeout"))
def test_industry_window_diagnostic_does_not_turn_published_prices_into_pending(failure) -> None:
    messages: list[ProgressMessage] = []
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0, report=SyncProgress(messages.append, lambda: 0.0))
    status = replace(
        _failed_status("fixture"), state="completed", reason=None, snapshot_hash="a" * 64,
        industry_readiness=HistoryIndustryReadiness(4402, 4402, 0, failure_category=failure),
    )
    reporter.publish_result(status)

    output = _compact_log(RenderProgress(messages[-1], color=False))
    assert "完成 | 同步完成 | 历史行业整窗覆盖 0/4402 股" in output
    assert "训练行业未就绪" not in output
    assert not status.needs_retry
    assert ("行业核验异常" in output) is (failure is not None)
    if failure is not None:
        assert f"行业核验异常 {failure}" in output
    assert all(field.level is ProgressLevel.WARNING for field in messages[-1].fields
               if field.text.startswith(("历史行业整窗覆盖 ", "行业核验异常 ")))


def test_context_and_industry_share_one_waiting_line_and_one_final_completion() -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    output = SyncProgress(messages.append, lambda: clock[0])
    reporter = StderrHistorySyncProgress(monotonic=lambda: clock[0], report=output)
    reporter.publish(HistorySyncProgress("loading_context", "started", 0, 1))
    reporter.publish(HistorySyncProgress("supplier_industry", "started", 0, 4402, "capco", data_source="中上协"))
    for second in range(5, 76, 5):
        clock[0] = float(second)
        output.heartbeat()
    waiting = [message for message in messages if message.state == "等待"]
    assert len(waiting) == 2
    assert all(message.task is not None and message.task.key == "supplier_industry" for message in waiting)
    assert all("来源 中上协" in RenderProgress(message) for message in waiting)

    clock[0] = 79.0
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 1, 4402, "000001",
                                         cache_hit=True, data_source="本地（巨潮原件）"))
    clock[0] = 85.0
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 4402, 4402, "689009",
                                         cache_hit=True, data_source="本地（巨潮原件）"))
    assert messages[-1].state == "处理中" and not messages[-1].finished
    clock[0] = 86.0
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 4402, 4402, "capco",
                                         industry_missing_codes=4402, data_source="本地（中上协证据）"))
    assert not any(message.state == "完成" for message in messages)
    reporter.publish(HistorySyncProgress("loading_context", "completed", 1, 1))
    completion = messages[-2]
    assert completion.task is not None and completion.task.key == "supplier_industry"
    assert completion.state == "完成" and completion.finished
    assert "来源 本地（中上协证据）" in RenderProgress(completion)
    assert messages[-1].task is not None and messages[-1].task.key == "loading_context"
    assert messages[-1].state == "完成"
    count = len(messages)
    clock[0] = 120.0
    output.heartbeat()
    assert len(messages) == count


@pytest.mark.parametrize("terminal", ("failed", "cancelled"))
def test_context_failure_or_cancellation_discards_deferred_industry_completion(terminal) -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    output = SyncProgress(messages.append, lambda: clock[0])
    reporter = StderrHistorySyncProgress(monotonic=lambda: clock[0], report=output)
    reporter.publish(HistorySyncProgress("loading_context", "started", 0, 1))
    reporter.publish(HistorySyncProgress("supplier_industry", "started", 0, 1, "capco", data_source="中上协"))
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 1, 1, "capco"))
    reporter.publish(HistorySyncProgress("loading_context", terminal, 0, 1))
    assert not any(message.state == "完成" for message in messages)
    count = len(messages)
    clock[0] = 60.0
    output.heartbeat()
    assert len(messages) == count


@pytest.mark.parametrize("with_context", (False, True))
def test_independent_industry_completion_does_not_require_a_capco_marker(with_context) -> None:
    messages: list[ProgressMessage] = []
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0, report=SyncProgress(messages.append, lambda: 0.0))
    if with_context:
        reporter.publish(HistorySyncProgress("loading_context", "started", 0, 1))
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 1, 1, "301548", data_source="独立来源"))
    if with_context:
        assert messages[-1].state == "处理中"
        reporter.publish(HistorySyncProgress("loading_context", "completed", 1, 1))
    completed = [message for message in messages if message.task is not None
                 and message.task.key == "supplier_industry" and message.state == "完成"]
    assert len(completed) == 1
    assert "来源 独立来源" in RenderProgress(completed[0])


def test_late_industry_failure_is_not_overwritten_by_a_deferred_success() -> None:
    messages: list[ProgressMessage] = []
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0, report=SyncProgress(messages.append, lambda: 0.0))
    reporter.publish(HistorySyncProgress("loading_context", "started", 0, 1))
    reporter.publish(HistorySyncProgress("supplier_industry", "completed", 1, 1, "capco"))
    reporter.publish(HistorySyncProgress("supplier_industry", "failed", 0, 1, "capco",
                                         failure_detail="official_industry_budget_exhausted"))
    assert messages[-1].state == "失败"
    assert "official_industry_budget_exhausted" in RenderProgress(messages[-1])
    reporter.publish(HistorySyncProgress("loading_context", "completed", 1, 1))
    assert not any(message.task is not None and message.task.key == "supplier_industry"
                   and message.state == "完成" for message in messages)


def test_legacy_resume_progress_is_visible_with_counts_and_month(capsys):
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    reporter.publish(HistorySyncProgress("history_gap_resume", "waiting", 64, 65, current_item="2026-09"))
    reporter.publish(HistorySyncProgress("history_gap_resume", "completed", 65, 65, current_item="2026-09"))
    output = _compact_log(capsys.readouterr().err)
    assert "旧断点恢复 00:00:00 | 等待 | 64/65" in output
    assert "旧断点恢复 00:00:00 | 完成 | 65/65" in output
    assert "处理项 2026-09" in output


def test_complete_code_reuse_is_not_reported_as_another_network_request(capsys):
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    reporter.publish(HistorySyncProgress("tencent_history", "completed", 1, 2, "600001"))
    output = _compact_log(capsys.readouterr().err)
    assert "无新增或缺口，复用" in output
    assert "本次请求 0日" in output and "处理 本地复用" in output
    assert "本次覆盖 未重新核验" in output and "来源 Tencent" not in output


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
    output = _compact_log(capsys.readouterr().err).splitlines()
    assert "处理中" in output[0] and "2/4" in output[0]
    assert "完成" in output[1] and "4/4" in output[1]
    assert "合格 3 | 曾ST排除 1 | 未知排除 0" in output[1]


def test_supplier_preparation_waiting_and_retry_are_reported(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 978.061, 978.061))

    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "waiting",
            0,
            1,
            current_item="301548",
            attempt=3,
            max_attempts=3,
            call_elapsed_seconds=10.849,
        )
    )
    assert "行业快照 00:00:00 | 等待 | 0/1" in _compact_log(capsys.readouterr().err)

    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "retrying",
            0,
            1,
            current_item="301548",
            attempt=3,
            max_attempts=3,
        )
    )

    assert (
        _compact_log(capsys.readouterr().err)
        == "00:14:38 | history·行业快照 00:00:00 | 重试 | 0/1 (0.00%) | 股票代码 301548 | 尝试 3/3\n"
    )


def test_industry_progress_reports_stock_counts_and_cache_without_duplicate_starts(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0, 1, 1, 1, 2, 3))
    started = HistorySyncProgress("supplier_industry", "started", 4, 18, "301548")
    progress.publish(started)
    progress.publish(started)
    progress.publish(HistorySyncProgress("supplier_industry", "waiting", 4, 18, "301548", call_elapsed_seconds=1))
    progress.publish(HistorySyncProgress("supplier_industry", "waiting", 4, 18, "301548", call_elapsed_seconds=5))
    progress.publish(HistorySyncProgress("supplier_industry", "completed", 5, 18, "301548", cache_hit=True))
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "开始 | 4/18" in lines[0]
    assert "处理中 | 5/18" in lines[-1] and "缓存复用" in lines[-1]
    assert all("行业快照" in line for line in lines)


def test_unlocated_failure_does_not_blame_last_successful_stock(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)
    progress.publish(HistorySyncProgress("tencent_history", "completed", 2603, 4401, "600361"))
    progress.publish_result(_failed_status("history_runtime_failed"))
    line = _compact_log(capsys.readouterr().err).splitlines()[-1]
    assert "故障位置未知" in line
    assert "600361" not in line


def test_industry_missing_coverage_remains_visible_on_cache_hit(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)
    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "completed",
            3,
            18,
            "2019-07-01",
            cache_hit=True,
            industry_missing_codes=2,
        )
    )
    line = _compact_log(capsys.readouterr().err)
    assert "缓存复用 | 缺行业 2 股" in line


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

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "股票下载 00:00:00 | 开始" in lines[0]
    assert any("未复权日线" in line and "等待" in line for line in lines)
    assert "13/100" in lines[-1] and "处理中" in lines[-1]


def test_partition_sealing_reports_start_and_completion_without_wait_heartbeats(capsys) -> None:
    clock = [100.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    clock[0] = 101
    progress.publish(HistorySyncProgress("sealing_partitions", "started", 0, 1, "prices_2026.sqlite3"))
    clock[0] = 106
    progress.publish(HistorySyncProgress("sealing_partitions", "waiting", 0, 1, "prices_2026.sqlite3"))
    progress._output.heartbeat()
    clock[0] = 127
    progress.publish(HistorySyncProgress("sealing_partitions", "completed", 1, 1, "prices_2026.sqlite3"))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert len(lines) == 2
    assert "数据库文件校验 00:00:00 | 开始" in lines[0]
    assert "数据库文件校验 00:00:26 | 完成" in lines[1]
    assert all("prices_2026.sqlite3" in line for line in lines)


def test_publication_checks_keep_stage_elapsed_across_months(capsys) -> None:
    clock = [100.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    clock[0] = 101
    progress.publish(HistorySyncProgress("checking_partitions", "started", 0, 2, "2026-01"))
    clock[0] = 106
    progress.publish(HistorySyncProgress("checking_partitions", "waiting", 0, 2, "2026-01"))
    progress._output.heartbeat()
    clock[0] = 107
    progress.publish(HistorySyncProgress("checking_partitions", "completed", 1, 2, "2026-01"))
    clock[0] = 108
    progress.publish(HistorySyncProgress("checking_partitions", "started", 1, 2, "2026-02"))
    clock[0] = 113
    progress.publish(HistorySyncProgress("checking_partitions", "waiting", 1, 2, "2026-02"))
    progress._output.heartbeat()
    clock[0] = 114
    progress.publish(HistorySyncProgress("checking_partitions", "completed", 2, 2, "2026-02"))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert len(lines) == 3
    assert "发布分区检查 00:00:00 | 开始 | 0/2" in lines[0]
    assert "月份 2026-01" in lines[0]
    assert "发布分区检查 00:00:06 | 处理中 | 1/2" in lines[1]
    assert "月份 2026-02" in lines[-1]
    assert "发布分区检查 00:00:13 | 完成 | 2/2" in lines[-1]


@pytest.mark.parametrize("stage,items", [
    ("checking_partitions", ("2026-01", "2026-02")),
    ("sealing_partitions", ("prices_2025.sqlite3", "prices_2026.sqlite3")),
])
def test_verification_prints_every_changed_count_even_within_one_second(stage, items, capsys):
    clock = [0.0]
    report = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    report.publish(HistorySyncProgress(stage, "started", 0, 2, items[0]))
    report.publish(HistorySyncProgress(stage, "started", 0, 2, items[0]))
    clock[0] = 0.1
    report.publish(HistorySyncProgress(stage, "completed", 1, 2, items[0]))
    report.publish(HistorySyncProgress(stage, "completed", 1, 2, items[0]))
    report.publish(HistorySyncProgress(stage, "started", 1, 2, items[1]))
    clock[0] = 0.2
    report.publish(HistorySyncProgress(stage, "completed", 2, 2, items[1]))
    report.publish(HistorySyncProgress(stage, "completed", 2, 2, items[1]))
    clock[0] = 10
    report._output.heartbeat()
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert len(lines) == 3
    assert [line.split(" | ")[3].split()[0] for line in lines] == ["0/2", "1/2", "2/2"]
    assert sum("| 开始 |" in line for line in lines) == 1
    assert "| 完成 |" in lines[-1]


@pytest.mark.parametrize("stage", ("checking_partitions", "sealing_partitions"))
def test_verification_waiting_with_a_new_count_is_reported_once(stage, capsys):
    clock = [0.0]
    report = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    report.publish(HistorySyncProgress(stage, "started", 0, 2))
    clock[0] = 0.1
    report.publish(HistorySyncProgress(stage, "waiting", 1, 2))
    report.publish(HistorySyncProgress(stage, "waiting", 1, 2))
    clock[0] = 10
    report._output.heartbeat()
    report.publish(HistorySyncProgress(stage, "completed", 2, 2))
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert len(lines) == 3 and "| 等待 | 1/2" in lines[1]


@pytest.mark.parametrize("stage", ("checking_partitions", "sealing_partitions"))
@pytest.mark.parametrize("state,label", (("failed", "失败"), ("retrying", "重试"), ("cancelled", "取消")))
def test_verification_diagnostics_are_immediate_even_without_count_changes(stage, state, label, capsys):
    report = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    report.publish(HistorySyncProgress(stage, "started", 0, 2))
    report.publish(HistorySyncProgress(stage, state, 0, 2,
                                       failure_reason="fixture_verification_error" if state == "failed" else None))
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert len(lines) == 2 and f"| {label} |" in lines[-1]
    if state == "failed":
        assert "错误 fixture_verification_error" in lines[-1]


@pytest.mark.parametrize("stage", ("checking_partitions", "sealing_partitions"))
def test_empty_verification_still_reports_its_real_completion(stage, capsys):
    report = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    report.publish(HistorySyncProgress(stage, "started", 0, 0))
    report.publish(HistorySyncProgress(stage, "completed", 0, 0))
    assert "| 完成 |" in _compact_log(capsys.readouterr().err).splitlines()[-1]


def test_publication_preparation_and_snapshot_start_are_not_silent(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0, 1, 6, 7, 8))

    progress.publish(HistorySyncProgress("preparing_publication", "started", 0, 1))
    progress.publish(HistorySyncProgress("preparing_publication", "waiting", 0, 1))
    progress.publish(HistorySyncProgress("preparing_publication", "completed", 1, 1))
    progress.publish(HistorySyncProgress("publishing_snapshot", "started", 0, 1))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "发布准备 00:00:00 | 开始" in lines[0]
    assert "发布准备 00:00:05 | 等待" in lines[1]
    assert "发布准备 00:00:06 | 完成" in lines[2]
    assert "发布快照 00:00:00 | 开始" in lines[3]


def test_partition_persistence_reports_start_heartbeat_and_completion(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0.0, 0.0, 1.0, 6.0, 7.0))

    progress.publish(HistorySyncProgress("persisting_partitions", "started", 0, 3, "2026-07"))
    progress.publish(HistorySyncProgress("persisting_partitions", "completed", 1, 3, "2026-07"))
    progress.publish(HistorySyncProgress("persisting_partitions", "started", 1, 3, "2026-08"))
    progress.publish(HistorySyncProgress("persisting_partitions", "completed", 3, 3, "2026-09"))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "正式月库提交 00:00:00 | 开始" in lines[0]
    assert "月份 2026-07" in lines[0]
    assert "正式月库提交 00:00:07 | 完成 | 3/3" in lines[-1]
    assert "月份 2026-09" in lines[-1]


def test_generic_sqlite_waiting_does_not_register_a_duplicate_heartbeat(capsys) -> None:
    clock = [0.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])

    progress.publish(HistorySyncProgress("persisting_partitions", "waiting", 0, 1))
    clock[0] = 30.0
    progress._output.heartbeat()

    assert capsys.readouterr().err == ""
    progress.publish(HistorySyncProgress("persisting_partitions", "waiting", 0, 1, "2026-07"))
    progress.publish(HistorySyncProgress(
        "persisting_partitions", "failed", 0, 1, failure_reason="disk_full", failure_detail="sqlite_full",
    ))
    output = _compact_log(capsys.readouterr().err)
    assert "月份 2026-07" in output
    assert "失败" in output and "disk_full" in output and "sqlite_full" in output


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

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "结果 失败" in lines[0] and "history_tencent_overlap_conflict" in lines[0]
    assert "已确认 29/4400" in lines[0]
    assert "Tencent下载" in lines[-1] and "其它失败 5 股" in lines[-1]


def test_tencent_diagnostics_keep_coverage_and_defer_durations_to_summary(capsys) -> None:
    clock = [100.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    pipeline = HistoryPipelineProgress(404, 16, 843.67, 2300.15, 2258.73)
    progress.publish(HistorySyncProgress("tencent_history", "started", 0, 4400))
    clock[0] = 1486.0
    progress.publish(
        HistorySyncProgress(
            "tencent_history",
            "completed",
            388,
            4400,
            "000952",
            supplier_source="tencent",
            requested_sessions=2000,
            price_coverage=HistoryPriceCoverage(2000, 1980, 1980, 1980),
            pipeline=pipeline,
        )
    )

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "history·Tencent下载 00:00:00 | 已确认 0/4400 | 开始" in lines[0]
    assert "00:23:06 | history·Tencent下载 00:23:06" in lines[-1]
    assert "结果 部分" in lines[-1] and "raw 1980/2000" in lines[-1] and "缺日 20" in lines[-1]
    assert "已确认 388/4400" in lines[-1]
    assert "累计" not in lines[-1] and "回压" not in lines[-1]
    clock[0] = 1487.0
    progress.publish(HistorySyncProgress(
        "tencent_history", "completed", 4400, 4400,
        tencent_summary=HistoryTencentSummary(4400, 4371, 29, 24), pipeline=pipeline,
    ))
    summary = _compact_log(capsys.readouterr().err)
    assert "完整 4371 股" in summary and "其它失败 5 股" in summary
    assert "网络累计 00:14:03" in summary and "落库累计 00:38:20" in summary
    assert "恢复读库累计" in summary and "回压 00:37:38" in summary


def test_tencent_waiting_only_prints_compact_pipeline_counts(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    progress.publish(
        HistorySyncProgress(
            "tencent_history",
            "waiting",
            176,
            4401,
            pipeline=HistoryPipelineProgress(
                192,
                16,
                206.0,
                1083.0,
                1240.0,
                inflight_codes=8,
                pending_codes=4201,
                oldest_inflight_seconds=50.0,
                read_seconds=12.0,
                validation_seconds=3.0,
                unconfirmed_write_seconds=8.0,
            ),
        )
    )

    output = capsys.readouterr().err
    assert "history·Tencent下载" in output
    compact = _compact_log(output)
    assert "已确认 176/4401" in compact
    assert "请求 0 待写 16 写入 0" in compact
    assert "读库/校验" in compact
    assert all(text not in output for text in ("累计", "回压", "在途", "待派发", "本批待确认"))


def test_pipeline_separators_align_across_digit_counts_states_and_stock_names(capsys) -> None:
    clock = [0.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])
    for count, name, state in ((9, "宝兰德", "waiting"), (100, "云涌科技", "completed"), (9999, "AB测试", "waiting")):
        progress.publish(HistorySyncProgress(
            "tencent_history", state, count, 12000, "688058",
            current_name=name, supplier_source="tencent", requested_sessions=2000,
            pipeline=HistoryPipelineProgress(count + 1, 16, 1.0, 2.0, 3.0, writing_codes=8,
                                             network_completed_codes=count),
        ))
        clock[0] += 5.0
    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    positions = [tuple(DisplayWidth(line[:match.start()]) for match in re.finditer(r"\|", line)) for line in lines]
    assert positions[0] == positions[1] == positions[2]
    assert all(len(line.split(" | ")) == 7 for line in lines)


def test_failed_result_keeps_the_specific_supplier_stage_and_error_code(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 145.0, 145.0, 1070.569))
    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "failed",
            0,
            1,
            current_item="301548",
            attempt=3,
            max_attempts=3,
            call_elapsed_seconds=45.0,
        )
    )
    progress.publish(HistorySyncProgress("loading_context", "failed", 0, 1))

    progress.publish_result(_failed_status("supplier_industry_timeout"))

    line = _compact_log(capsys.readouterr().err).splitlines()[-1]
    assert "history·结果汇总" in line and "失败" in line
    assert "行业快照" in line and "股票代码 301548" in line
    assert "supplier_industry_timeout" in line


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
    progress.publish(HistorySyncProgress("supplier_industry", "completed", 1, 1, current_item="capco"))
    progress.publish(HistorySyncProgress("loading_context", "failed", 0, 1))

    progress.publish_result(_failed_status("history_sync_failed"))

    line = _compact_log(capsys.readouterr().err).splitlines()[-1]
    assert "同步失败 | 加载上下文" in line and "history_sync_failed" in line
    assert "行业快照" not in line


def test_completed_result_keeps_machine_json_out_of_the_progress_stream(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 101.0))
    completed = _failed_status("supplier_industry_timeout")
    completed = HistoryMaintenanceStatus(
        "completed",
        None,
        completed.history_root,
        completed.selected_baseline_source,
        None,
        "a" * 64,
        date(2026, 9, 10),
        date(2026, 9, 9),
    )

    progress.publish_result(completed)

    assert _compact_log(capsys.readouterr().err) == "00:00:01 | history·结果汇总 00:00:00 | 完成 | 同步完成\n"


@pytest.mark.parametrize(
    "name", ("宝兰德", "云涌科技", "*ST测试", "ＡB测试", "e\u0301测试", None, "超长公司名称超过八个汉字")
)
@pytest.mark.parametrize(
    "result,actual", (("合格", 2000), ("部分", 19), ("待重试", None), ("失败", None), ("无需下载", 0))
)
def test_stock_fields_remain_compact_across_names_and_counts(name, result, actual) -> None:
    progress = _stock_progress(
        command="sync market",
        completed=4400,
        name=name,
        result=result,
        action="本地检查",
        raw=actual,
        qfq=actual,
        paired=actual,
        missing=None if actual is None else 2000 - actual,
        missing_dates=(date(2026, 10, 12),),
        reason="a_long_failure_reason",
    )
    message = FormatStockDownloadProgress(progress)
    plain = RenderProgress(message)
    assert DisplayWidth(message.fields[1].text) == DisplayWidth("股票 688058 ") + 8
    assert not re.search(r" {2,}\|", plain.replace(message.fields[1].text, "股票"))
    assert "raw " in plain and "qfq " in plain and "完整配对 " in plain
    assert "\n" not in plain and "\t" not in plain
    assert plain.index("原因") < plain.index("缺失日期")
    if name is not None and DisplayWidth(name) > 8:
        assert "…" in plain and f"完整名称 {name}" in plain


@pytest.mark.parametrize(
    "result,missing,escape",
    (
        ("合格", 0, "\x1b[32m"),
        ("复用", 0, None),
        ("无需下载", 0, None),
        ("部分", 1, "\x1b[33m"),
        ("待重试", None, "\x1b[33m"),
        ("失败", None, "\x1b[31m"),
    ),
)
def test_color_preserves_exact_plain_layout_and_resets_each_field(result, missing, escape) -> None:
    message = FormatStockDownloadProgress(_stock_progress(result=result, missing=missing))
    plain = RenderProgress(message)
    colored = RenderProgress(message, color=True)
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain
    assert colored.count("\x1b[0m") == len(re.findall(r"\x1b\[3[123]m", colored))
    if escape:
        assert escape + "结果 " in colored
    else:
        assert "\x1b" not in colored


@pytest.mark.parametrize(
    "tty,no_color,term,expected",
    (
        (True, "", "xterm", True),
        (True, "1", "xterm", False),
        (True, "0", "xterm", False),
        (True, "", "dumb", False),
        (False, "", "xterm", False),
    ),
)
def test_terminal_color_policy_and_single_write(monkeypatch, tty, no_color, term, expected) -> None:
    monkeypatch.setenv("NO_COLOR", no_color)
    monkeypatch.setenv("TERM", term)
    stream = _Terminal(tty)
    report = TerminalDownloadProgress(stream)
    report(FormatStockDownloadProgress(_stock_progress()))
    assert ("\x1b[32m" in stream.getvalue()) is expected
    assert stream.writes == 1 and stream.getvalue().count("\n") == 1


def test_untrusted_fields_cannot_inject_ansi_newlines_or_columns() -> None:
    message = FormatStockDownloadProgress(
        _stock_progress(
            name="宝\n兰\t德|\x1b[31m",
            reason="bad\rreason|\x1b]0;title\x07",
        ),
    )
    line = RenderProgress(message)
    assert line.count("|") == 8
    assert all(char not in line for char in ("\x1b", "\n", "\r", "\t", "\x07"))
    assert "title" not in line and "[31m" not in line


def test_history_stock_output_keeps_compact_fields_across_coverage_and_result(monkeypatch) -> None:
    stream = _Terminal(True)
    monkeypatch.setattr("sys.stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    reporter = StderrHistorySyncProgress(monotonic=_Clock(0, 1, 2, 3))
    for completed, code, name, coverage, reason in (
        (9, "688058", "宝兰德", HistoryPriceCoverage(2000, 2000, 2000, 2000), None),
        (10, "688060", "云涌科技", HistoryPriceCoverage(2000, 19, 19, 19), None),
        (100, "600001", "*ST测试", None, "history_tencent_overlap_conflict"),
    ):
        reporter.publish(
            HistorySyncProgress(
                "tencent_history",
                "failed" if reason else "completed",
                completed,
                4400,
                code,
                current_name=name,
                supplier_source="tencent",
                requested_sessions=2000,
                price_coverage=coverage,
                failure_reason=reason,
            )
        )
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    for line, color in zip(lines, ("32", "33", "31"), strict=True):
        assert f"\x1b[{color}m结果 " in line
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", line) for line in lines]
    assert all(not re.search(r" {2,}\|", line.split(" | 处理 ", 1)[-1]) for line in plain)
    assert all("raw " in line and "完整配对 " in line for line in plain)


def test_qfq_write_failure_is_red_but_retry_stays_yellow(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = _Terminal(True)
    progress = QfqDownloadProgress(date(2026, 10, 13), "test", 2, TerminalDownloadProgress(stream))
    security = BaoStockSecurity("688058", "宝兰德", "star", date(2019, 1, 1), None, "test")
    progress.record_pending(security, RuntimeError("timeout"), (), action="Tencent下载")
    progress.record_pending(security, OSError("disk_full"), (), action="Tencent下载", failure_level=ProgressLevel.ERROR)
    progress.finish(False)
    lines = stream.getvalue().splitlines()
    assert "\x1b[33m结果 待重试" in lines[0]
    assert "\x1b[31m结果 待重试" in lines[1] and "\x1b[31m原因 disk_full" in lines[1]
    assert "\x1b[31mtimeout" in lines[2]
    assert "\x1b[31m已处理" in lines[-1]


def test_terminal_also_sanitizes_direct_message_fields() -> None:
    assert RenderProgress(ProgressMessage((ProgressField("unsafe\n|\x1b[31m"),))) == "unsafe"


def test_empty_population_and_large_numbers_are_never_truncated() -> None:
    empty = FormatStockDownloadProgress(_stock_progress(total=0, completed=0, target=0, raw=0, qfq=0, paired=0))
    assert "100.00%" in RenderProgress(empty)
    large = FormatStockDownloadProgress(_stock_progress(total=1000000, completed=1000000))
    assert "1000000/1000000" in RenderProgress(large)
    assert RenderProgress(WithProgressTime(large, 360000, 360000)).startswith("100:00:00")


def test_missing_dates_keep_bounded_preview_in_tail() -> None:
    dates = tuple(date(2026, 10, day) for day in range(1, 11))
    line = RenderProgress(FormatStockDownloadProgress(_stock_progress(missing=10, missing_dates=dates)))
    assert "缺失日期 2026-10-01" in line and "2026-10-10" in line
    assert "2026-10-09" in line and "2026-10-10" in line


def test_history_summary_uses_final_gap_state_after_repair(monkeypatch) -> None:
    stream = _Terminal(True)
    monkeypatch.setattr("sys.stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0)
    reporter.publish(
        HistorySyncProgress(
            "tencent_history",
            "completed",
            1,
            1,
            "688058",
            supplier_source="tencent",
            requested_sessions=2000,
            price_coverage=HistoryPriceCoverage(2000, 1999, 1999, 1999),
        )
    )
    status = replace(_failed_status("fixture"), state="completed", reason=None)
    reporter.publish_result(status)
    last = stream.getvalue().splitlines()[-1]
    assert "\x1b[32m同步完成" in last and "待重试记录" not in last
    assert status.state == "completed" and status.unresolved_price_cells == 0


def test_qfq_failed_local_extraction_never_reports_success() -> None:
    from trader.download.infra.qfq_progress import QfqLocalProgress

    messages = []
    with pytest.raises(OSError, match="disk_full"):
        with QfqLocalProgress(messages.append):
            raise OSError("disk_full")
    last = RenderProgress(messages[-1], color=True)
    assert "中断" in last and "完成" not in last and "\x1b[32m" not in last
