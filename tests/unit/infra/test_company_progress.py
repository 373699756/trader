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
    CompanyDateProgress,
    CompanyEvidenceSyncResult,
    CompanyStockProgress,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    EvidenceSyncProgress,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, ReportCompanyStock
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
    first, final = stream.getvalue().splitlines()
    assert "已尝试 2/147 篇 | 已提取正文 1 篇" in first
    assert "当前正文股票 600001 示例" in first and "已分析 0" not in first and "待分析 0" not in first
    assert "累计 00:09:51" in first and "591.1秒" not in first
    assert "已尝试 147/147 篇 | 已提取正文 147 篇" in final and "状态 完成" in final


def test_unknown_total_and_cache_reuse_are_not_reported_as_partial_downloads():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0))
    report(EvidenceSyncProgress("financial", 0, 0, 0, 0, 1, status="reused"))
    unknown, reused = stream.getvalue().splitlines()
    assert "进度 0/? 页（总数待确认）" in unknown
    assert "无需请求，复用有效缓存" in reused
    assert "状态 本地复用" in reused and "100.00%" not in reused


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
    output = stream.getvalue().splitlines()[-1]
    assert "已核对日期 0/1 天（全部目标市场）" in output
    assert "当前日期 2026-10-24 | 上交所 | 本查询第 3/4 页" in output
    assert "4401" not in output and "600001" not in output
    assert "原始资料变更" not in output and "变更行" not in output
    assert "已分析" not in output and "待分析" not in output and "事件待核验" not in output
    assert "证券组" not in output and "分片" not in output
    assert "本阶段 00:00:00" in output and "命令累计 00:35:10" in output
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
    published = stream.getvalue().splitlines()[-1]
    assert "已核验股票 2/4401 股（等待原子发布）" in published
    assert "当前 600001 示例 | 公告 12 篇 | 已提取正文 4 篇 | 待提取正文 8 篇" in published
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

    first, second, third, fourth, final = stream.getvalue().splitlines()
    assert "原因 公告正文：PDF无法解析" in first
    assert "已提取正文 153 篇" in second and "原因" not in second
    assert "原因 公告正文：PDF超过大小上限" in third and "PDF无法解析" not in third
    assert "已提取正文 156 篇" in fourth and "原因" not in fourth
    assert "PDF无法解析；公告正文：PDF超过大小上限" in final
    assert "状态 待续传" in final


def test_terminal_and_json_show_pending_units_without_claiming_full_coverage(monkeypatch, capsys):
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(
        EvidenceSyncProgress(
            "complete", 4402, 26, 127, 2589, 2, "announcements:timeout", status="deferred", pending_documents=3
        )
    )
    output = stream.getvalue()
    assert "已发布 4402 股（含待核实） | 公告待补 26 个范围 | 待补正文 3 篇" in output
    assert "原始资料变更 2589 条（非每股发布行）" in output
    assert "下次 sync company 从已提交断点续传" in output
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


def test_last_stock_starts_atomic_publication_and_news_without_candidates_is_explicit():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    ReportCompanyStock(report, (("600001", "示例"),), CompanyStockProgress("600001", processed=1, total=1))
    report(EvidenceSyncProgress("publish", 1, 0, 2, 3, 0, status="complete"))
    report(EvidenceSyncProgress("news", 0, 0, 2, 3, 0, status="complete"))
    facts, pending, published, news = stream.getvalue().splitlines()
    assert "逐股事件核验" in facts and "当前 600001 示例" in facts
    assert "正在原子提交（尚未发布）" in pending
    assert "已发布 1/1 股" in published and "状态 完成" in published
    assert "无候选名单，本轮未抓取新闻" in news and "状态 未覆盖" in news


def test_source_deadline_and_unreadable_documents_have_separate_recovery_messages():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("risk_documents", 2, 5, 0, 0, 0, failed_documents=1, deferred_documents=2))
    report(
        EvidenceSyncProgress(
            "complete",
            1,
            2,
            3,
            4,
            0,
            "announcements:deadline,risk_documents:disclosure_document_no_text",
            status="deferred",
            pending_documents=5,
        )
    )
    text = stream.getvalue()
    assert "本轮失败 1 篇 | 冷却暂缓 2 篇" in text
    assert "公告目录：阶段预算用尽，保留断点" in text
    assert "公告正文：PDF未提取到文字" in text and "无文本或短文本公告保持待核实" in text
