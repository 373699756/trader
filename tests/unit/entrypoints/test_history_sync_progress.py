from __future__ import annotations

import re
from dataclasses import replace
from datetime import date
from io import StringIO
from pathlib import Path

import pytest

from trader.download.application.progress_message import DisplayWidth, ProgressField, ProgressLevel, ProgressMessage
from trader.download.application.qfq_download_progress import QfqDownloadProgress
from trader.download.application.stock_download_progress import FormatStockDownloadProgress, StockDownloadProgress
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_price_coverage import HistoryPriceCoverage
from trader.download.domain.history_sync import (
    HistoryPipelineProgress,
    HistoryStEligibilitySummary,
    HistorySyncProgress,
    HistoryTencentSummary,
)
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.terminal_progress import RenderProgress, TerminalDownloadProgress


def _compact_log(value: str) -> str:
    value = re.sub(r" +", " ", value)
    return re.sub(r" */ *", "/", re.sub(r"([（(]) +", r"\1", value))


class _Clock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


def test_industry_retry_and_incomplete_publication_are_explicit(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    progress.publish(
        HistorySyncProgress(
            "supplier_industry",
            "retrying",
            0,
            45,
            "2026-09-09",
            attempt=2,
            max_attempts=3,
        )
    )
    progress.publish_result(
        replace(_failed_status("fixture"), state="completed", reason=None, unresolved_price_cells=2000)
    )
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert "0/45" in lines[0] and "尝试 2/3" in lines[0]
    assert "同步完成 | 价格缺口待补 | 未修复价格缺口 2000 股日" in lines[1]


def test_legacy_resume_progress_is_visible_with_counts_and_month(capsys):
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    reporter.publish(HistorySyncProgress("history_gap_resume", "waiting", 64, 65, current_item="2026-09"))
    reporter.publish(HistorySyncProgress("history_gap_resume", "completed", 65, 65, current_item="2026-09"))
    output = _compact_log(capsys.readouterr().err)
    assert "旧断点恢复 | 等待 | 64/65" in output
    assert "旧断点恢复 | 完成 | 65/65" in output
    assert "处理项 2026-09" in output


def test_complete_code_reuse_is_not_reported_as_another_network_request(capsys):
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    reporter.publish(HistorySyncProgress("tencent_history", "completed", 1, 2, "600001"))
    output = _compact_log(capsys.readouterr().err)
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
    output = _compact_log(capsys.readouterr().err).splitlines()
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
    assert "行业快照 | 等待 | 0/1" in _compact_log(capsys.readouterr().err)

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

    assert (
        _compact_log(capsys.readouterr().err)
        == "00:14:38 | 行业快照 | 重试 | 0/1 (0.00%) | 日期 2021-01-04 | 尝试 3/3\n"
    )


def test_industry_progress_reports_date_counts_and_cache_without_duplicate_starts(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0, 1, 1, 1, 2, 3))
    started = HistorySyncProgress("supplier_industry", "started", 4, 18, "2020-07-01")
    progress.publish(started)
    progress.publish(started)
    progress.publish(HistorySyncProgress("supplier_industry", "waiting", 4, 18, "2020-07-01", call_elapsed_seconds=1))
    progress.publish(HistorySyncProgress("supplier_industry", "waiting", 4, 18, "2020-07-01", call_elapsed_seconds=5))
    progress.publish(HistorySyncProgress("supplier_industry", "completed", 5, 18, "2020-07-01", cache_hit=True))
    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert len(lines) == 3
    assert "开始 | 4/18" in lines[0]
    assert "等待 | 4/18" in lines[1]
    assert "完成 | 5/18" in lines[2] and "缓存复用" in lines[2]


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

    assert (
        _compact_log(capsys.readouterr().err)
        == "00:00:35 | 股票下载 | 完成 | 13/100 (13.00%) | 股票 600001 | 阶段耗时 00:00:35\n"
    )


def test_partition_sealing_prints_the_first_month_before_long_local_work(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 101.0))

    progress.publish(HistorySyncProgress("sealing_partitions", "started", 0, 100, current_item="2018-07"))

    assert (
        _compact_log(capsys.readouterr().err)
        == "00:00:01 | 发布内容校验 | 开始 | 0/100 (0.00%) | 月份 2018-07 | 本月耗时 00:00:00\n"
    )


def test_partition_persistence_reports_start_heartbeat_and_completion(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0.0, 0.0, 1.0, 6.0, 7.0))

    progress.publish(HistorySyncProgress("persisting_partitions", "started", 0, 3, "2026-07"))
    progress.publish(HistorySyncProgress("persisting_partitions", "completed", 1, 3, "2026-07"))
    progress.publish(HistorySyncProgress("persisting_partitions", "started", 1, 3, "2026-08"))
    progress.publish(HistorySyncProgress("persisting_partitions", "completed", 3, 3, "2026-09"))

    assert _compact_log(capsys.readouterr().err).splitlines() == [
        "00:00:00 | 正式月库提交 | 开始 | 0/3 (0.00%) | 月份 2026-07 | 本月耗时 00:00:00",
        "00:00:06 | 正式月库提交 | 开始 | 1/3 (33.33%) | 月份 2026-08 | 本月耗时 00:00:00",
        "00:00:07 | 正式月库提交 | 完成 | 3/3 (100.00%) | 月份 2026-09 | 本月耗时 00:00:00",
    ]


def test_partition_persistence_placeholder_waiting_is_suppressed(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(0.0, 21.0))

    progress.publish(HistorySyncProgress("persisting_partitions", "waiting", 0, 1))

    assert _compact_log(capsys.readouterr().err) == ""


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

    assert _compact_log(capsys.readouterr().err).splitlines() == [
        "00:00:00 | download 股票进度 | 29/4400（0.66%） | 股票 600001 — | 处理 Tencent下载 | 结果 失败 "
        "| raw -/2000 | qfq -/2000 | 完整配对 -/2000 | 缺日 未知 "
        "| 网络 -/4400 | 落库 29/4400 | 待写 - | 下载 --:--:-- | 落库耗时 --:--:-- | 回压 --:--:-- "
        "| 总计 00:00:00 | 原因 history_tencent_overlap_conflict",
        "00:00:00 | Tencent下载 | 完成 | 4400/4400 | 完整 4371 股 | 部分 0 股 | 失败 29 股 "
        "| 其中待重试 0 股 | 其中本地复用 0 股 "
        "| 重叠冲突 24 股 | 其它失败 5 股 | 总计 00:00:00",
    ]


def test_tencent_completion_combines_compact_coverage_pipeline_and_durations(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=_Clock(100.0, 100.0, 1486.0))
    progress.publish(HistorySyncProgress("tencent_history", "started", 0, 4400))
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
            pipeline=HistoryPipelineProgress(404, 16, 843.67, 2300.15, 2258.73),
        )
    )

    assert _compact_log(capsys.readouterr().err) == (
        "00:23:06 | download 股票进度 | 388/4400（8.82%） | 股票 000952 — | 处理 Tencent下载 | 结果 部分 "
        "| raw 1980/2000 | qfq 1980/2000 | 完整配对 1980/2000 | 缺日 20 "
        "| 网络 404/4400 | 落库 388/4400 | 待写 16 | 下载 00:14:03 "
        "| 落库耗时 00:38:20 | 回压 00:37:38 | 总计 00:23:06 | 原因 无\n"
    )


def test_tencent_waiting_state_does_not_reserve_a_wide_column(capsys) -> None:
    progress = StderrHistorySyncProgress(monotonic=lambda: 0.0)
    progress.publish(
        HistorySyncProgress(
            "tencent_history",
            "waiting",
            176,
            4401,
            pipeline=HistoryPipelineProgress(192, 16, 206.0, 1083.0, 1240.0),
        )
    )

    output = capsys.readouterr().err
    assert "Tencent下载" in output
    assert "| 等待 |" in output
    assert "网络" in output and "落库" in output and "回压" in output


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

    assert _compact_log(capsys.readouterr().err).splitlines()[-1] == (
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

    assert _compact_log(capsys.readouterr().err).splitlines()[-1] == (
        "00:00:40 | 同步失败 | 加载上下文 | 错误 history_sync_failed"
    )


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

    assert _compact_log(capsys.readouterr().err) == "00:00:01 | 同步完成\n"


def _stock_progress(**changes) -> StockDownloadProgress:
    return replace(
        StockDownloadProgress(
            "download",
            9,
            4400,
            "688058",
            "宝兰德",
            "Tencent下载",
            "合格",
            2000,
            2000,
            2000,
            2000,
            0,
        ),
        **changes,
    )


@pytest.mark.parametrize(
    "name", ("宝兰德", "云涌科技", "*ST测试", "ＡB测试", "e\u0301测试", None, "超长公司名称超过八个汉字")
)
@pytest.mark.parametrize(
    "result,actual", (("合格", 2000), ("部分", 19), ("待重试", None), ("失败", None), ("无需下载", 0))
)
def test_stock_fields_remain_compact_across_names_and_counts(name, result, actual) -> None:
    progress = _stock_progress(
        command="download_qfq",
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
    message = FormatStockDownloadProgress(0, progress)
    plain = RenderProgress(message)
    assert DisplayWidth(message.fields[3].text) == DisplayWidth("股票 688058 ") + 8
    assert not re.search(r" {2,}\|", plain.replace(message.fields[3].text, "股票"))
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
    message = FormatStockDownloadProgress(1, _stock_progress(result=result, missing=missing))
    plain = RenderProgress(message)
    colored = RenderProgress(message, color=True)
    assert re.sub(r"\x1b\[[0-9;]*m", "", colored) == plain
    assert colored.count("\x1b[0m") == len(re.findall(r"\x1b\[3[123]m", colored))
    if escape:
        assert escape + "结果 " in colored
    else:
        assert "\x1b" not in colored


class _Terminal(StringIO):
    def __init__(self, tty: bool):
        super().__init__()
        self.tty = tty
        self.writes = 0

    def isatty(self):
        return self.tty

    def write(self, text):
        self.writes += 1
        return super().write(text)


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
    report(FormatStockDownloadProgress(0, _stock_progress()))
    assert ("\x1b[32m" in stream.getvalue()) is expected
    assert stream.writes == 1 and stream.getvalue().count("\n") == 1


def test_untrusted_fields_cannot_inject_ansi_newlines_or_columns() -> None:
    message = FormatStockDownloadProgress(
        0,
        _stock_progress(
            name="宝\n兰\t德|\x1b[31m",
            reason="bad\rreason|\x1b]0;title\x07",
        ),
    )
    line = RenderProgress(message)
    assert line.count("|") == 10
    assert all(char not in line for char in ("\x1b", "\n", "\r", "\t", "\x07"))
    assert "title" not in line and "[31m" not in line


def test_history_stock_output_keeps_compact_fields_across_coverage_and_result(monkeypatch) -> None:
    stream = _Terminal(True)
    monkeypatch.setattr("sys.stderr", stream)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0)
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
    progress = QfqDownloadProgress(date(2026, 10, 13), "test", 2, TerminalDownloadProgress(stream), lambda: 0)
    security = BaoStockSecurity("688058", "宝兰德", "star", date(2019, 1, 1), None, "test")
    progress.record_pending(security, RuntimeError("timeout"), (), action="Tencent下载")
    progress.record_pending(security, OSError("disk_full"), (), action="Tencent下载", failure_level=ProgressLevel.ERROR)
    progress.finish(False)
    lines = stream.getvalue().splitlines()
    assert "\x1b[33m结果 待重试" in lines[0]
    assert "\x1b[31m结果 待重试" in lines[1] and "\x1b[31m原因 disk_full" in lines[1]
    assert "\x1b[31mqfq 待重试原因" in lines[2]
    assert "\x1b[31mqfq 结束" in lines[-1]


def test_terminal_also_sanitizes_direct_message_fields() -> None:
    assert RenderProgress(ProgressMessage((ProgressField("unsafe\n|\x1b[31m"),))) == "unsafe"


def test_empty_population_and_large_numbers_are_never_truncated() -> None:
    empty = FormatStockDownloadProgress(0, _stock_progress(total=0, completed=0, target=0, raw=0, qfq=0, paired=0))
    assert "100.00%" in RenderProgress(empty)
    large = FormatStockDownloadProgress(360000, _stock_progress(total=1000000, completed=1000000))
    assert "1000000/1000000" in RenderProgress(large)
    assert RenderProgress(large).startswith("100:00:00")


def test_missing_dates_keep_bounded_preview_in_tail() -> None:
    dates = tuple(date(2026, 10, day) for day in range(1, 11))
    line = RenderProgress(FormatStockDownloadProgress(0, _stock_progress(missing=10, missing_dates=dates)))
    assert "缺失日期 2026-10-01" in line and "等 10 个交易日" in line
    assert "2026-10-09" not in line and "2026-10-10" not in line


def test_history_summary_retains_partial_warning_without_changing_result(monkeypatch) -> None:
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
    assert "\x1b[33m同步完成" in last and "本轮有不完整或待重试记录" in last
    assert status.state == "completed" and status.unresolved_price_cells == 0


def test_qfq_failed_local_extraction_never_reports_success() -> None:
    from trader.download.infra.qfq_progress import QfqLocalProgress

    messages = []
    with pytest.raises(OSError, match="disk_full"):
        with QfqLocalProgress(messages.append):
            raise OSError("disk_full")
    last = RenderProgress(messages[-1], color=True)
    assert "中断" in last and "完成" not in last and "\x1b[32m" not in last
