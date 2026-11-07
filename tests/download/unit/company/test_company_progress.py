"""Risk download terminal and JSON distinguish stage work from published stocks."""

import json
from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader import bootstrap
from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.application.progress_message import ProgressField, ProgressMessage, ProgressTask
from trader.download.domain.company_evidence import (
    CompanyDateProgress,
    CompanyEvidenceSyncResult,
    CompanyPublicationProgress,
    CompanyStockProgress,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    EvidenceSyncProgress,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, ReportCompanyStock
from trader.entrypoints.cli import Main


def _messages(text: str) -> list[str]:
    """Group one atomic company frame while preserving standalone diagnostics."""
    lines = text.splitlines()
    messages = []
    index = 0
    while index < len(lines):
        size = 3 if "company·总览" in lines[index] else 1
        messages.append("\n".join(lines[index:index + size]))
        index += size
    return messages


@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (SyncResult((), failure="preparation_ReferenceDatabaseIncompatibleError"), "company_st.sqlite3 的结构不兼容"),
        (SyncResult((), failure="preparation_AkshareCalendarTimeoutError"), "交易日历准备超时"),
        (SyncResult((SyncTaskResult("company", "failed", "ProcessLockError"),)), "用 fg 恢复对应任务后按 Ctrl+C"),
    ),
)
def test_sync_failures_explain_recovery_without_polluting_json(monkeypatch, capsys, result, expected):
    monkeypatch.setattr(bootstrap, "ExecuteSync", lambda *args, **kwargs: result)
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
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
    report(
        EvidenceSyncProgress(
            "risk_documents",
            1,
            146,
            425,
            40179,
            0,
            attempted_documents=2,
            stock=CompanyStockProgress("600001", "示例"),
        )
    )
    clock[0] += 0.1
    report(EvidenceSyncProgress("risk_documents", 147, 0, 425, 40325, 0, status="complete", attempted_documents=147))
    first, final = _messages(stream.getvalue())
    assert "正文已入库1篇，可处理待补146篇" in first
    assert "当前股票600001-示例" in first and "已分析0/0篇" in first
    assert first.startswith("00:09:51 | company·总览") and "591.1秒" not in first
    assert "正文已入库147篇，可处理待补0篇" in final and "阶段完成" in final


def test_reference_progress_uses_the_shared_prefix_and_flushes_each_stage():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("st_eligibility", 1, 1, 0, 0, 0))
    report(EvidenceSyncProgress("st_eligibility", 2, 0, 0, 0, 0))
    report(EvidenceSyncProgress("calendar", 1, 0, 0, 0, 1, status="reused"))
    report.report_preparation(ProgressMessage((ProgressField("开始准备"),), task=ProgressTask("参考", "当前行业", "当前行业"), state="开始"))
    lines = stream.getvalue().splitlines()
    assert len(lines) == 4
    assert all(line.startswith("00:00:00 | 参考·") for line in lines)
    assert "结果 完成" in lines[1] and "结果 本地复用" in lines[2]
    assert "参考·当前行业" in lines[3] and "开始 | 开始准备" in " ".join(lines[3].split())


def test_unknown_total_and_cache_reuse_are_not_reported_as_partial_downloads():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0))
    report(EvidenceSyncProgress("financial", 0, 0, 0, 0, 1, status="reused"))
    unknown, reused = _messages(stream.getvalue())
    assert "财务已保存0/待发现页" in unknown
    assert "本地复用原来源缓存" in reused and "100.00%" not in reused


def test_batch_announcement_progress_uses_dates_and_fact_verification_uses_stocks():
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
            dates=CompanyDateProgress(day, day, 0),
        )
    )
    output = _messages(stream.getvalue())[-1]
    assert "全市场日期闭合0/1天" in output
    assert "上交所 当前第3/4页" in output
    assert "4401" not in output and "600001" not in output
    assert "原始资料变更" not in output and "变更行" not in output
    assert "已分析" not in output and "待分析" not in output and "事件待核验" not in output
    assert "证券组" not in output and "分片" not in output
    assert output.startswith("00:35:10 | company·总览") and "当前目录" in output
    report(
        EvidenceSyncProgress(
            "facts",
            2,
            4399,
            100,
            1000,
            124,
            stock=CompanyStockProgress(
                "600001", "示例", processed=2, total=4401, announcements=12, analyzed=4, pending=8
            ),
        )
    )
    published = _messages(stream.getvalue())[-1]
    assert "本批核验2/4401股" in published
    assert "600001-示例" in published and "核验未发布" in published
    assert "| 已发布" not in published


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

    first, second, third, fourth, final = _messages(stream.getvalue())
    assert "原因 公告正文：PDF无法解析" in first
    assert "正文已入库151篇，可处理待补1095篇" in second
    assert "原因 公告正文：PDF超过扩展大小上限" in third and "PDF无法解析" not in third
    assert "正文已入库156篇，可处理待补1090篇" in fourth and "原因" not in fourth
    assert "待续传" in final


def test_restart_displays_persistent_failures_separately_from_new_failures():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("risk_documents", 0, 7, 0, 0, 0, unresolved_document_failures=7))
    assert "未解决失败7篇" in stream.getvalue()


def test_terminal_and_json_show_pending_units_without_claiming_full_coverage(monkeypatch, capsys):
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(
        EvidenceSyncProgress(
            "complete", 4402, 26, 127, 2589, 2, "announcements:timeout", status="deferred", pending_documents=3
        )
    )
    output = stream.getvalue()
    assert "已发布 4402 股（含待核实） | 公告待补 26 个范围 | 可重试正文 3 篇" in output
    assert "原始资料变更 2589 条（非每股发布行）" in output
    assert "下次 sync company 从已提交断点续传" in output
    assert "100.00%" not in output and "待续传" in output
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
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 1
    payload = json.loads(capsys.readouterr().out)["tasks"][0]
    assert payload["pending_documents"] == 3 and payload["pending_partitions"] == 26
    assert payload["pending_counts_checked"] is True
    assert payload["elapsed_seconds"] == 314.09


def test_cancelled_terminal_and_json_report_unknown_counts_without_claiming_zero_pending(monkeypatch, capsys):
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress(
        "complete", 1, None, 8, 3, 0, "task:cancelled", status="deferred",
        pending_documents=None, deferred_documents=None, ocr_documents=None, unextractable_documents=None,
    ))
    output = stream.getvalue()
    assert "已发布 1 股（含待核实）" in output
    assert "公告待补数量未核对" in output and "可重试正文数量未核对" in output
    assert "中断后未重新核对待处理数，不表示无剩余任务" in output
    assert "可重试正文 0" not in output and "公告待补 0" not in output
    result = CompanyEvidenceSyncResult(
        datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        2, 1, 8, 3, 0, None, ("task:cancelled",), 42.0,
        pending_documents=None, ocr_documents=None, unextractable_documents=None,
    )
    assert result.needs_retry and replace(result, failures=()).needs_retry
    monkeypatch.setattr(
        bootstrap, "ExecuteSync",
        lambda *args, **kwargs: SyncResult((SyncTaskResult("company", "cancelled", company=result),), cancelled=True),
    )
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 130
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "cancelled"
    company = payload["tasks"][0]
    assert company["published_codes"] == 1 and company["fetched_pages"] == 8
    assert company["pending_partitions"] is company["pending_documents"] is None
    assert company["ocr_documents"] is company["unextractable_documents"] is None
    assert company["pending_counts_checked"] is False


def test_last_stock_closes_verification_without_claiming_a_publication():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    ReportCompanyStock(report, (("600001", "示例"),), CompanyStockProgress("600001", processed=1, total=1))
    assert "正式提交" not in stream.getvalue()
    ReportCompanyStock(report, (), CompanyPublicationProgress("started", ("600001",)))
    ReportCompanyStock(report, (), CompanyPublicationProgress("committed", ("600001",), ("600001",)))
    report(EvidenceSyncProgress("publish", 1, 0, 2, 3, 0, status="complete"))
    report(EvidenceSyncProgress("news", 0, 0, 2, 3, 0, status="complete"))
    facts, pending, published, finished, news = _messages(stream.getvalue())
    assert "来源本地" in facts and "600001-示例" in facts
    assert "正式提交中，尚未发布" in pending
    assert "本批提交1股" in published and "本批已提交" in published
    assert "正式覆盖" in finished and "本轮提交1股" in finished
    assert "无候选，未抓取新闻" in news and "本轮未覆盖" in news


def test_source_failure_and_unreadable_documents_have_separate_recovery_messages():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(
        EvidenceSyncProgress(
            "risk_documents",
            2,
            5,
            0,
            0,
            0,
            failed_documents=1,
            deferred_documents=2,
            unextractable_documents=1,
        )
    )
    report(
        EvidenceSyncProgress(
            "complete",
            1,
            2,
            3,
            4,
            0,
            "announcements:source_stopped,risk_documents:disclosure_document_no_text",
            status="deferred",
            pending_documents=5,
            deferred_documents=2,
        )
    )
    text = stream.getvalue()
    normalized = " ".join(text.split())
    assert "本轮失败 1 篇" in normalized and "冷却 2 篇" in normalized
    assert "公告目录：来源暂缓（连续失败或限流）" in text
    assert "公告正文：PDF未提取到文字" in text
    assert "OCR仍失败的正文标记为不可提取/资料未知，同内容同策略不再重试" in text
