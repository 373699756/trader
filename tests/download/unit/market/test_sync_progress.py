from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
import os
import subprocess
import sys
import time
from threading import enumerate as enumerate_threads
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.shared.reference_fixtures import PreparedFixture
from trader.download.application.progress_message import ProgressField, ProgressMessage, ProgressTask
from trader.download.application.sync_progress import SyncProgress
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.history_sync import HistorySyncProgress
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.infra.terminal_progress import TerminalDownloadProgress
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.settings.data_paths import DataPaths
from trader.download.infra.download_failure_diagnostics import DownloadFailureDiagnostics
from trader.download.domain.download_failure import DownloadFailureContext
from trader.infra.http_contracts import HttpStatusError


def test_shared_clock_survives_interleaved_stocks_months_and_task_restarts():
    clock = [100.0]
    stream = StringIO()
    report = SyncProgress(TerminalDownloadProgress(stream), lambda: clock[0], started_at=0)
    prices = ProgressTask("history", "prices", "Tencent下载")
    writing = ProgressTask("history", "write", "价格写入")
    report(ProgressMessage((ProgressField("股票 000001"),), task=prices, state="开始"))
    clock[0] = 105
    report(ProgressMessage((ProgressField("月份 2026-01"),), task=writing, state="开始"))
    clock[0] = 110
    report(ProgressMessage((ProgressField("股票 000002"),), task=prices))
    report(ProgressMessage((ProgressField("月份 2026-02"),), task=writing))
    report(ProgressMessage((), task=prices, state="完成", finished=True))
    clock[0] = 120
    report(ProgressMessage((), task=replace(prices, execution=1), state="开始"))
    lines = stream.getvalue().splitlines()
    assert "00:01:50 | history·Tencent下载 00:00:10" in lines[2]
    assert "00:01:50 | history·价格写入 00:00:05" in lines[3]
    assert [part.strip() for part in lines[-1].split(" | ")] == [
        "00:02:00", "history·Tencent下载 00:00:00", "开始",
    ]


def test_heartbeat_uses_latest_real_counts_and_never_reopens_finished_tasks():
    clock = [0.0]
    stream = StringIO()
    report = SyncProgress(TerminalDownloadProgress(stream), lambda: clock[0])
    task = ProgressTask("history", "read", "读取下载任务")
    report(ProgressMessage((ProgressField("股票 0/4402"),), task=task, state="开始"))
    clock[0] = 0.1
    report(ProgressMessage((ProgressField("股票 512/4402"),), task=task))
    clock[0] = 5
    report.heartbeat()
    assert [part.strip() for part in stream.getvalue().splitlines()[-1].split(" | ")][2:4] == [
        "等待", "股票 512/4402",
    ]
    report(ProgressMessage((), task=task, state="完成", finished=True))
    report(ProgressMessage((ProgressField("迟到事件"),), task=task))
    before = stream.getvalue()
    clock[0] = 20
    report.heartbeat()
    assert stream.getvalue() == before


@pytest.mark.parametrize("fail", [False, True])
def test_heartbeat_thread_is_reclaimed_after_success_and_exception(fail):
    report = SyncProgress(lambda message: None, time.monotonic)
    try:
        with report.keep_alive():
            assert any(thread.name == "download-progress" for thread in enumerate_threads())
            if fail:
                raise RuntimeError("fixture")
    except RuntimeError:
        assert fail
    assert not any(thread.name == "download-progress" for thread in enumerate_threads())


def test_unknown_task_and_invalid_start_are_explicit_errors():
    report = SyncProgress(lambda message: None, lambda: 1)
    with pytest.raises(ValueError, match="task_missing"):
        report(ProgressMessage(()))
    with pytest.raises(ValueError, match="start_invalid"):
        SyncProgress(lambda message: None, lambda: 1, started_at=2)


def test_failure_console_keeps_operation_budget_and_http_code_without_payload():
    stream = StringIO()
    report = SyncProgress(TerminalDownloadProgress(stream), lambda: 0)
    records = []
    failure = DownloadFailureDiagnostics(records.append, lambda: datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
                                         report=report, owner="company")
    error = RuntimeError("private_payload_must_not_appear")
    error.__cause__ = HttpStatusError(503)
    failure(DownloadFailureContext("document_download", code="000001", supplier="cninfo", attempt=2,
                                    maximum_attempts=3, timeout_seconds=30, elapsed_seconds=12), error)
    line = stream.getvalue()
    assert "company·失败诊断" in line and "实际操作 document_download" in line
    assert "HTTP_503" in line and "尝试 2/3" in line and "单次超时 30秒" in line
    assert "private_payload" not in line and len(records) == 1


def test_shell_feedback_uses_shared_prefix_and_sanitizes_installer_lines():
    source = Path(__file__).resolve().parents[4] / "src/trader/download/application/progress_message.py"
    result = subprocess.run(
        [sys.executable, str(source), "--pipe"], input="安装依赖\n\x1b[31m外部输出 | 子字段\n", text=True,
        capture_output=True, timeout=10,
        env={**os.environ, "TRADER_SYNC_STARTED_MONOTONIC": str(time.monotonic() - 70)},
    )
    assert result.returncode == 0 and result.stdout == ""
    assert all("启动·运行环境检查 00:01:" in line for line in result.stderr.splitlines())
    assert "\x1b" not in result.stderr
    assert all(line.count("|") == 3 for line in result.stderr.splitlines())


@pytest.mark.parametrize("target,fill", [("all", False), ("history", False), ("market", False), ("company", False),
                                        ("history", True), ("market", True), ("company", True)])
def test_sync_entrypoints_share_timing_and_keep_business_results(monkeypatch, tmp_path, target, fill):
    from trader.bootstrap import ExecuteSync

    root = Path(__file__).resolve().parents[4]
    settings = replace(LoadRuntimeSettings(root / "config/runtime.json"), data_paths=DataPaths(tmp_path / "data"))
    now = datetime(2026, 11, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    clock = [100.0]
    stream = StringIO()
    log = SyncProgress(TerminalDownloadProgress(stream), lambda: clock[0], started_at=0)
    reference = PreparedFixture(now)

    def prepare(*args, report, **kwargs):
        report(ProgressMessage((), task=ProgressTask("参考", "test", "夹具准备"), state="开始"))
        clock[0] += 10
        report(ProgressMessage((), task=ProgressTask("参考", "test", "夹具准备"), state="完成", finished=True))
        return reference

    def history(*args, progress, repair_only, **kwargs):
        assert repair_only is fill
        progress.publish(HistorySyncProgress("tencent_history", "started", 0, 1))
        clock[0] += 10
        return HistoryMaintenanceStatus("completed", None, tmp_path, "tencent", None, "a" * 64, now.date(), now.date())

    def market(*args, report, repair_only, **kwargs):
        assert repair_only is fill
        report(ProgressMessage((), task=ProgressTask("market", "prices", "价格更新"), state="开始"))
        clock[0] += 10
        return QfqUpdateResult(now.date(), 1)

    def company(*args, **kwargs):
        kwargs_progress = args[1]
        from trader.download.domain.company_evidence import EvidenceSyncProgress

        kwargs_progress(EvidenceSyncProgress("facts", 1, 0, 0, 0, 0, status="complete"))
        clock[0] += 10
        return CompanyEvidenceSyncResult(now, 1, 1, 0, 0, 0, 0, (), 0)

    monkeypatch.setattr("trader.bootstrap.LoadRuntimeSettings", lambda path: settings)
    monkeypatch.setattr("trader.bootstrap._prepare_reference", prepare)
    monkeypatch.setattr("trader.bootstrap.ExecuteHistoryDownload", history)
    monkeypatch.setattr("trader.bootstrap.ExecuteQfqDownload", market)
    monkeypatch.setattr("trader.bootstrap._sync_company_evidence", company)
    monkeypatch.setattr("trader.bootstrap.RecentPublishedNewsCodes", lambda *args: ())
    result = ExecuteSync(settings.config_path, target, fill_missing=fill, report=log)
    assert result.exit_code == 0
    selected = ["history", "market", "company"] if target == "all" else [target]
    assert [item.task for item in result.tasks] == selected
    lines = stream.getvalue().splitlines()
    totals = [line.split(" | ")[0] for line in lines]
    assert totals == sorted(totals)
    assert all("·" in line.split(" | ")[1] for line in lines)
    assert not (tmp_path / "data").exists()
