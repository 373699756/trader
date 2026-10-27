"""Risk download terminal and JSON distinguish stage work from published stocks."""

import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader import bootstrap
from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.domain.company_evidence import (
    CompanyEvidenceSyncResult,
    CompanyStockProgress,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    EvidenceSyncProgress,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress
from trader.entrypoints.cli import Main


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (SyncResult((), failure="preparation_AkshareCalendarTimeoutError"), "交易日历准备超时"),
        (SyncResult((SyncTaskResult("company", "failed", "ProcessLockError"),)), "用 fg 恢复对应任务后按 Ctrl+C"),
    ),
)
def test_sync_failures_explain_recovery_without_polluting_json(monkeypatch, capsys, result, expected):
    monkeypatch.setattr(bootstrap, "ExecuteSync", lambda *args, **kwargs: result)
    config = Path(__file__).resolve().parents[3] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == "data_sync" and payload["status"] == "failed"
    assert expected in captured.err and expected not in captured.out


def test_elapsed_hms_and_final_progress_are_printed_even_inside_throttle_window():
    clock = [0.0]
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    clock[0] = 591.1
    report(EvidenceSyncProgress("risk_documents", 1, 146, 425, 40179, 0, attempted_documents=2))
    clock[0] += 0.1
    report(EvidenceSyncProgress("risk_documents", 147, 0, 425, 40325, 0, status="complete", attempted_documents=147))
    first, final = stream.getvalue().splitlines()
    assert "已尝试 2/147 篇 | 已核验 1 篇" in first
    assert "累计 00:09:51" in first and "591.1秒" not in first
    assert "已尝试 147/147 篇 | 已核验 147 篇" in final and "状态 完成" in final


def test_unknown_total_and_cache_reuse_are_not_reported_as_partial_downloads():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0))
    report(EvidenceSyncProgress("financial", 0, 0, 0, 0, 1, status="reused"))
    unknown, reused = stream.getvalue().splitlines()
    assert "进度 0/? 页（总数待确认）" in unknown
    assert "无需请求，复用有效缓存" in reused
    assert "状态 本地复用" in reused and "100.00%" not in reused


def test_announcement_progress_separates_complete_dates_current_pages_and_stage_time():
    clock = [0.0]
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    clock[0] = 2100
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0))
    clock[0] = 2110
    day = datetime(2026, 10, 24).date()
    scope = DisclosurePartition(
        "sse",
        day,
        day,
        page=3,
        expected_rows=93,
        query=DisclosureQuery("shmb", (DisclosureSecurity("600001", "issuer"),)),
    )
    report(
        EvidenceSyncProgress(
            "announcements",
            0,
            36,
            100,
            1000,
            124,
            scope=scope,
            stock=CompanyStockProgress(
                "600001", "示例", processed=2, total=4401, announcements=12, analyzed=4, pending=8
            ),
        )
    )
    output = stream.getvalue().splitlines()[-1]
    assert "本轮涉及公告 2/4401 股" in output
    assert "最近处理公告目录 600001 示例" in output
    assert output.index("公告增量下载") < output.index("最近处理公告目录 600001 示例") < output.index("本轮涉及公告")
    assert "本轮该股页记录 12 篇 | 正文分析未开始" in output
    assert "原始资料变更 1000 条（非每股发布行）" in output and "变更行" not in output
    assert "已分析" not in output and "待分析" not in output and "事件待核验" not in output
    assert "第3/4页" not in output and "证券组" not in output and "分片" not in output
    assert "本阶段 00:00:00" in output and "命令累计 00:35:10" in output


def test_document_progress_reports_each_new_failure_once_and_summarizes_at_end():
    clock = [0.0]
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    unreadable = "risk_documents:disclosure_document_unreadable"
    oversized = "risk_documents:disclosure_document_oversized"

    report(EvidenceSyncProgress("risk_documents", 151, 1095, 1028, 23107, 2, unreadable))
    clock[0] = 2.1
    report(EvidenceSyncProgress("risk_documents", 153, 1093, 1028, 23109, 2, unreadable))
    clock[0] = 4.2
    report(EvidenceSyncProgress("risk_documents", 154, 1092, 1028, 23110, 2, f"{unreadable},{oversized}"))
    clock[0] = 6.3
    report(EvidenceSyncProgress("risk_documents", 156, 1090, 1028, 23112, 2, f"{unreadable},{oversized}"))
    clock[0] = 6.4
    report(
        EvidenceSyncProgress(
            "risk_documents", 156, 1090, 1028, 23112, 2, f"{unreadable},{oversized}", status="deferred"
        )
    )

    first, second, third, fourth, final = stream.getvalue().splitlines()
    assert f"原因 {unreadable}" in first
    assert "已核验 153 篇" in second and "原因" not in second
    assert f"原因 {oversized}" in third and unreadable not in third
    assert "已核验 156 篇" in fourth and "原因" not in fourth
    assert f"原因 {unreadable},{oversized}" in final and "状态 待续传" in final


def test_terminal_and_json_show_pending_units_without_claiming_full_coverage(monkeypatch, capsys):
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(
        EvidenceSyncProgress(
            "complete", 4402, 26, 127, 2589, 2, "announcements:timeout", status="deferred", pending_documents=3
        )
    )
    output = stream.getvalue()
    assert "已发布 4402 股（含待核实） | 公告覆盖待补 | 待补正文 3 篇" in output
    assert "100.00%" not in output and "状态 待续传" in output
    result = CompanyEvidenceSyncResult(
        datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        4402,
        4402,
        127,
        2589,
        2,
        26,
        ("announcements:timeout",),
        314.09,
        3,
    )
    monkeypatch.setattr(
        bootstrap,
        "ExecuteSync",
        lambda *args, **kwargs: SyncResult((SyncTaskResult("company", "pending", company=result),)),
    )
    config = Path(__file__).resolve().parents[3] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 1
    payload = json.loads(capsys.readouterr().out)["tasks"][0]
    assert payload["pending_documents"] == 3 and payload["pending_partitions"] == 26
    assert payload["elapsed_seconds"] == 314.09
