from dataclasses import replace
from datetime import date
from io import StringIO
import re

from trader.download.application.sync_progress import SyncProgress
from trader.download.domain.history_sync import HistoryPipelineProgress, HistoryStockRequest, HistorySyncProgress
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.infra.progress_format import DisplayWidth


def _setup(base: int = 1050):
    clock = [0.0]
    stream = StringIO()
    report = SyncProgress(TerminalDownloadProgress(stream), lambda: clock[0])
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0], report=report)
    progress.publish(HistorySyncProgress("tencent_history", "started", base, 4402))
    return clock, stream, report, progress


def _request(code: str, state: str = "started") -> HistorySyncProgress:
    request = HistoryStockRequest(code, "测试股份", date(2026, 1, 1), date(2026, 1, 2), state)
    return HistorySyncProgress("tencent_history", "waiting", 1050, 4402, code, request=request)


def test_each_request_is_immediate_but_eight_slow_requests_share_one_wait_line():
    clock, stream, report, progress = _setup()
    for position in range(8):
        progress.publish(_request(f"{position + 1:06d}"))
    assert len(stream.getvalue().splitlines()) == 9
    assert "请求 8" in stream.getvalue().splitlines()[-1]
    for now in (4.9, 5, 5, 9.9, 10):
        clock[0] = now
        report.heartbeat()
    waiting = [line for line in stream.getvalue().splitlines() if "请求等待" in line]
    assert len(waiting) == 2
    assert all("等待000001-测试股份" in line for line in waiting)
    assert "请求等待5秒" in waiting[0] and "请求等待10秒" in waiting[1]


def test_network_return_is_not_confirmation_and_durable_stocks_are_all_reported():
    clock, stream, report, progress = _setup()
    progress.publish(_request("000001"))
    before = stream.getvalue()
    progress.publish(_request("000001", "finished"))
    assert stream.getvalue() == before
    pipeline = HistoryPipelineProgress(1052, 0, 0, 0, 0)
    for count, code in ((1051, "000001"), (1052, "000002")):
        progress.publish(HistorySyncProgress("tencent_history", "completed", count, 4402, code,
                                           current_name="测试股份", pipeline=pipeline,
                                           supplier_source="tencent", requested_sessions=2))
    lines = stream.getvalue().splitlines()
    assert "确认000001-测试股份" in lines[-2]
    assert "确认000002-测试股份" in lines[-1]
    clock[0] = 5
    report.heartbeat()
    assert "请求 0" in stream.getvalue().splitlines()[-1]


def test_reuse_and_write_wait_do_not_invent_supplier_requests():
    clock, stream, report, progress = _setup()
    pipeline = HistoryPipelineProgress(1051, 8, 0, 0, 0, inflight_codes=8, writing_codes=8, write_operation="价格写入")
    progress.publish(HistorySyncProgress("tencent_history", "waiting", 1050, 4402, pipeline=pipeline))
    clock[0] = 5
    report.heartbeat()
    line = stream.getvalue().splitlines()[-1]
    assert "价格写入" in line and "请求 0" in line and "写入 8" in line
    progress.publish(HistorySyncProgress("tencent_history", "completed", 1051, 4402, "000001", pipeline=replace(pipeline, queued_codes=0, writing_codes=0)))
    assert "复用确认000001" in stream.getvalue().splitlines()[-1]


def test_resume_baseline_is_excluded_from_speed_and_stalled_eta_expires():
    clock, stream, report, progress = _setup()
    clock[0] = 60
    progress.publish(HistorySyncProgress("tencent_history", "completed", 1060, 4402, "000001",
                                       pipeline=HistoryPipelineProgress(1060, 0, 0, 0, 0)))
    assert "600股/时" in stream.getvalue().splitlines()[-1]
    clock[0] = 240
    report.heartbeat()
    assert "速度/剩余估算中" in stream.getvalue().splitlines()[-1]


def test_request_confirmation_and_wait_columns_align_in_display_cells():
    clock, stream, report, progress = _setup()
    progress.publish(_request("000001"))
    clock[0] = 5
    report.heartbeat()
    progress.publish(HistorySyncProgress("tencent_history", "completed", 1051, 4402, "000001",
                                       current_name="超长股票名称ABCDEFG", pipeline=HistoryPipelineProgress(1051, 0, 0, 0, 0)))
    positions = [tuple(DisplayWidth(line[:mark.start()]) for mark in re.finditer(r"\|", line))
                 for line in stream.getvalue().splitlines()]
    assert len(set(positions)) == 1


def test_same_stock_failure_is_reported_once_before_its_durable_failure_confirmation():
    clock, stream, report, progress = _setup()
    progress.publish(_request("000001"))
    failure = replace(_request("000001", "finished"), state="failed", failure_reason="timeout")
    progress.publish(failure)
    progress.publish(replace(failure, request=None, completed_units=1051,
                             pipeline=HistoryPipelineProgress(1051, 0, 0, 0, 0)))
    assert stream.getvalue().count("原因 timeout") == 1
    assert "失败确认000001" in stream.getvalue()
