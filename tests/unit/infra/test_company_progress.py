"""Risk download terminal and JSON distinguish stage work from published stocks."""

import json
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

from trader import bootstrap
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult, EvidenceSyncProgress
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress
from trader.entrypoints.cli import Main


def test_elapsed_hms_and_final_progress_are_printed_even_inside_throttle_window():
    clock = [0.0]
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    clock[0] = 591.1
    report(EvidenceSyncProgress("risk_documents", 1, 146, 425, 40179, 0))
    clock[0] += 0.1
    report(EvidenceSyncProgress("risk_documents", 147, 0, 425, 40325, 0, status="complete"))
    first, final = stream.getvalue().splitlines()
    assert "进度 1/147 篇（0.68%）" in first
    assert "累计 00:09:51" in first and "591.1秒" not in first
    assert "进度 147/147 篇（100.00%）" in final and "状态 完成" in final


def test_unknown_total_and_cache_reuse_are_not_reported_as_partial_downloads():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0))
    report(EvidenceSyncProgress("financial", 0, 0, 0, 0, 1, status="reused"))
    unknown, reused = stream.getvalue().splitlines()
    assert "进度 0/? 页（总数待确认）" in unknown
    assert "无需请求，复用有效缓存" in reused
    assert "状态 本地复用" in reused and "100.00%" not in reused


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
    assert "进度 153/1246 篇" in second and "原因" not in second
    assert f"原因 {oversized}" in third and unreadable not in third
    assert "进度 156/1246 篇" in fourth and "原因" not in fourth
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
    assert "已发布投影 4402 股 | 待补公告 26 分片 | 待补正文 3 篇" in output
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
    monkeypatch.setattr(bootstrap, "ExecuteRiskDownload", lambda *args, **kwargs: result)
    config = Path(__file__).resolve().parents[3] / "config/runtime.json"
    assert Main(["--config", str(config), "download_risk"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_documents"] == 3 and payload["pending_partitions"] == 26
    assert payload["elapsed_seconds"] == 314.09
