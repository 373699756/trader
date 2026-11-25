"""Risk download terminal and JSON distinguish stage work from published stocks."""

from dataclasses import replace
from datetime import datetime
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader import bootstrap
from trader.download.application.company_round_progress import (
    CompanyEventFieldCoverage, CompanyFieldOutcomeCounts, CompanyFormalCoverage, CompanyRequestProgress,
)
from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.application.progress_message import ProgressField, ProgressMessage, ProgressTask
from trader.download.domain.company_evidence import (
    CompanyDateProgress,
    CompanyPublicationProgress,
    CompanyStockProgress,
    DisclosurePartition,
    DisclosureQuery,
    DisclosureSecurity,
    EvidenceSyncProgress,
    StructuredCompanyQuery,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, CompanyRoundProgress, ReportCompanyStock
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.entrypoints.cli import Main
from trader.download.domain.company_sync import (
    CompanyDownloadOperation, CompanyStructuredDownloadProgress, CompanySyncFamily, CompanySyncFamilyResult, CompanySyncStepResult,
)
from trader.download.domain.company_source_records import CompanyFinancialBatchCoverage
from tests.helpers.download.company_round import CompanySyncFamilyFixture, CompanySyncRoundFixture


_ROUND_AT = datetime(2026, 11, 21, 11, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_company_round_start_has_heartbeat_and_finishes_with_owner():
    messages = []
    clock = [0.0]
    progress = SyncProgress(messages.append, lambda: clock[0])
    company_progress = CompanyRoundProgress(progress, lambda: clock[0])

    def operation():
        company_progress.start(4403)
        clock[0] = 10
        progress.heartbeat()

    progress.run("company", operation)
    assert len(messages) == 2 and messages[0].state == "开始" and messages[1].state == "等待"
    assert any("4403" in field.text for field in messages[0].fields)
    clock[0] = 100
    progress.heartbeat()
    assert len(messages) == 2


def test_company_round_unconfirmed_counts_are_not_none_or_zero():
    messages = []
    progress = SyncProgress(messages.append, lambda: 0.0)
    company_progress = CompanyRoundProgress(progress, lambda: 0.0)
    family = CompanySyncFamilyResult(CompanySyncFamily.FIELDS, 1, None, None,
        CompanySyncStepResult(True, False, None, 0, (), ()), unknown_attempts=None)
    company_progress.finish(CompanySyncRoundFixture(_ROUND_AT, families=(family,)))
    text = " | ".join(field.text for field in messages[0].fields)
    assert "累计核验未核对股次" in text and "预算已占用未核对，未知未核对" in text and "None" not in text
    assert "统计未核对" in text


@pytest.mark.parametrize("paused", (False, True))
def test_company_progress_projects_formal_and_event_gaps_during_same_round(paused):
    messages = []
    clock = [0.0]
    progress = CompanyRoundProgress(SyncProgress(messages.append, lambda: clock[0]), lambda: clock[0])
    coverage = CompanyFormalCoverage(
        stocks=4, financial_complete=0, latest_profit_values=1,
        pledge_with_value=0, pledge_without_event=1, unlock_with_event=0, unlock_without_event=1,
        announcement_complete=0, announcement_partial=1, verified_risk_stocks=0,
        financial_fields=(("basic_eps", 2), ("book_value_per_share", 0), ("revenue_growth_pct", 0),
            ("net_profit_growth_pct", 0), ("core_profit_growth_pct", 0), ("roe_pct", 0),
            ("parent_net_profit", 1), ("core_net_profit", 1)),
        other_field_counts=(
            ("actual_pct", CompanyEventFieldCoverage(2, 0, 0, 0, 1)),
            ("repurchase_purpose", CompanyEventFieldCoverage(0, 1, 1, 1, 2, missing_values=2)),
        ),
        reduction_field_counts=(),
    )
    counts = CompanyFieldOutcomeCounts("b" * 64, 2, 1, 1, 1, 3, 0, coverage)
    progress.start(4)
    progress.report_coverage("本轮开始", counts)
    clock[0] = 30.0
    counts = replace(counts, publication_hash="c" * 64, coverage=replace(coverage, financial_fields=(
        ("basic_eps", 3), *coverage.financial_fields[1:],
    )))
    progress.report_coverage("同步中", counts)
    result = replace(CompanySyncRoundFixture(_ROUND_AT, families=()), publication_hash=counts.publication_hash)
    clock[0] = 60.0
    if paused:
        result = replace(result, budget_exhausted=True, publication_hash="")
    progress.finish(result, fields=None if paused else counts)

    reports = tuple(message for message in messages if message.task.key == "coverage")
    assert len(reports) == 3 and reports[-1].finished
    expected_phases = ("本轮开始", "同步中", "本轮收尾")
    for index, (message, phase) in enumerate(zip(reports, expected_phases, strict=True)):
        header = " | ".join(field.text for field in message.fields)
        assert phase in header and "来源 本地正式公司库" in header
        assert "正式荐股资料缺口" + ("38/44（86.36%）" if index == 0 else "37/44（84.09%）") in header
        assert ("以下为00:00:30前已核对的正式快照" in header) is (paused and phase == "本轮收尾")
        rows = {row.label.removeprefix("正式库·"): " | ".join(field.text for field in row.fields) for row in message.rows}
        assert ("正式缺2/4（50.00%）" if index == 0 else "正式缺1/4（25.00%）") in rows["EPS"]
        assert "已核无质押1股" in rows["质押比例"] and "部分覆盖1/4（25.00%）" in rows["公告风险覆盖"]
        assert "无适用项" in rows["减持事件字段"]
        assert "缺值2/5（40.00%）" in rows["其他公告字段"] and "待补证2/5（40.00%）" in rows["其他公告字段"]
        assert "已证未披露1项，不适用3项" in rows["其他公告字段"]
        assert "缺值2/3（66.67%）" in rows["回购用途"] and "已核有值2项" in rows["回购或增持实际比例"]


def test_company_round_financial_observations_do_not_close_qualification_gaps():
    messages = []
    progress = CompanyRoundProgress(SyncProgress(messages.append, lambda: 0.0), lambda: 0.0)
    progress.set_financial_batch(CompanyFinancialBatchCoverage(
        population=4403,
        accepted_pages=89,
        expected_pages=89,
        accepted_raw_rows=44_498,
        expected_raw_rows=44_498,
        latest_observed_stocks=4403,
        latest_complete_stocks=4367,
        latest_profit_pair_stocks=4403,
        missing_fields=(("basic_eps", 7), ("revenue_growth_pct", 1), ("core_profit_growth_pct", 18), ("roe_pct", 11)),
    ))
    family = CompanySyncFamilyFixture(CompanySyncFamily.FINANCIAL, gaps=("latest_financial_population_unverified",))
    progress.finish(CompanySyncRoundFixture(_ROUND_AT, families=(family,)))

    source, status = messages
    text = " | ".join(field.text for field in source.fields)
    assert status.state == "待补证"
    assert "财务批量页89/89，原始行44498/44498" in text
    assert "财务源观察4403/4403股，最新报表八项有值4367股，归母与扣非同时有值4403股" in text
    assert "EPS缺7股、营收同比缺1股、扣非同比缺18股、ROE缺11股" in text
    assert "已观察财报原始缺值37/35224（0.11%）" in text and "观测报告有缺项36/4403（0.82%）" in text
    assert any("各股最新财报的全量覆盖待核" in field.text for field in status.fields)
    assert any(row.label == "来源库·EPS" and "有值4396/4403（99.84%）" in row.fields[-1].text for row in source.rows)


@pytest.mark.parametrize("terminal", (False, True))
def test_company_failure_is_visible_with_exact_gaps_before_progress_throttle(terminal):
    messages = []
    clock = [0.0]
    progress = SyncProgress(messages.append, lambda: clock[0])
    company_progress = CompanyRoundProgress(progress, lambda: clock[0])
    gaps = ("supplier_input_rejected", "latest_financial_population_unverified")
    company_progress.advance(CompanySyncFamilyFixture(CompanySyncFamily.FINANCIAL, complete=False, gaps=gaps))
    clock[0] = 0.1
    failure = "company_round_page_number_mismatch"
    result = CompanySyncFamilyFixture(
        CompanySyncFamily.FINANCIAL, complete=False,
        failure=failure, gaps=gaps,
    )
    if terminal:
        company_progress.finish(CompanySyncRoundFixture(_ROUND_AT, families=(result,)))
    else:
        company_progress.advance(result)
    assert len(messages) == 2 and messages[-1].state == "失败"
    text = " | ".join(field.text for field in messages[-1].fields)
    assert f"错误 {failure}" in text
    assert "缺口原因 supplier_input_rejected,latest_financial_population_unverified" in text
    assert "来源返回内容未通过校验" in text
    assert "本族请求预算已占用0" in text


def test_company_interim_reasons_are_tracked_by_family_and_restored_at_finish():
    messages = []
    clock = [0.0]
    progress = CompanyRoundProgress(SyncProgress(messages.append, lambda: clock[0]), lambda: clock[0])
    gaps = ("population_members_became_ineligible", "new_population_members_wait_for_refresh")
    financial = CompanySyncFamilyFixture(CompanySyncFamily.FINANCIAL, complete=False, can_continue=True,
        gaps=gaps, accepted_inputs=12, source_calls=3)
    pledge = replace(financial, family=CompanySyncFamily.PLEDGE)

    def display(family: CompanySyncFamilyResult, *, elapsed: float = 5.0) -> tuple[str, ...]:
        clock[0] += elapsed
        before = len(messages)
        progress.advance(family)
        assert len(messages) == before + 1
        return tuple(field.text for field in messages[-1].fields)

    for first in (display(financial), display(pledge, elapsed=0.1)):
        assert any("缺口2项" in text for text in first)
        assert any(text.startswith("缺口原因 ") and all(gap in text for gap in gaps) for text in first)
        assert any(text.startswith("说明 ") for text in first)

    reordered = replace(financial, steps=2, accepted_inputs=36, source_calls=4,
        result=replace(financial.result, gaps=tuple(reversed(gaps))))
    repeated = display(reordered)
    assert "最近确认调度步" in repeated
    assert any("累计接纳输入36项" in text and "预算已占用4，未知0" in text for text in repeated)
    assert not any("缺口" in text or text.startswith("说明 ") for text in repeated)

    latest_gaps = (gaps[0], "latest_financial_population_unverified")
    latest = replace(reordered, result=replace(reordered.result, gaps=latest_gaps))
    changed = display(latest, elapsed=0.1)
    assert any(text.startswith("缺口原因 ") and all(gap in text for gap in latest_gaps) for text in changed)
    assert any(text.startswith("说明 ") and "各股最新财报的全量覆盖待核" in text for text in changed)

    resolved = replace(pledge, result=replace(pledge.result, gaps=()))
    cleared = display(resolved, elapsed=0.1)
    assert any("缺口0项" in text for text in cleared)
    assert not any(text.startswith("缺口原因 ") for text in cleared)
    assert not any("缺口" in text or text.startswith("说明 ") for text in display(latest))

    progress.finish(CompanySyncRoundFixture(_ROUND_AT, families=(latest, resolved)))
    final, _pledge_final = messages[-2:]
    assert final.finished and final.task.key == "financial"
    terminal = tuple(field.text for field in final.fields)
    assert any(text.startswith("缺口原因 ") and all(gap in text for gap in latest_gaps) and gaps[1] not in text
        for text in terminal)
    assert any(text.startswith("说明 ") and "各股最新财报的全量覆盖待核" in text for text in terminal)


@pytest.mark.parametrize("failure", (None, "source_attempt_unconfirmed"))
def test_company_step_heartbeat_repeats_only_progress_and_stops_with_owner(failure):
    messages = []
    clock = [0.0]
    progress = SyncProgress(messages.append, lambda: clock[0])
    company_progress = CompanyRoundProgress(progress, lambda: clock[0])
    company_progress.start(4403)
    family = CompanySyncFamilyResult(CompanySyncFamily.FINANCIAL, 2, 293, 3,
        CompanySyncStepResult(False, failure is None, accepted_inputs=293,
            gaps=("latest_financial_population_unverified",), failure=failure), unknown_attempts=1)
    clock[0] = 2
    company_progress.advance(family)
    assert any("最近确认调度步" == field.text for field in messages[-1].fields)
    assert any("累计接纳输入293项" in field.text and "占用3，未知1" in field.text for field in messages[-1].fields)
    assert any(field.text.startswith("缺口原因 ") for field in messages[-1].fields)
    if failure is not None:
        assert messages[-1].state == "阻塞"
        assert any(field.text == f"错误 {failure}" for field in messages[-1].fields)
    with company_progress.activity(CompanySyncFamily.PLEDGE):
        clock[0] = 6
        progress.heartbeat()
        assert len(messages) == 2
        clock[0] = 8
        progress.heartbeat()
        assert messages[-1].state == "处理中"
        assert any("当前执行 东财质押 已用 00:00:06" in field.text for field in messages[-1].fields)
    clock[0] = 14
    progress.heartbeat()
    assert len(messages) == 4 and messages[-1].state == "等待"
    for heartbeat in messages[-2:]:
        assert any("累计接纳输入293项" in field.text and "占用3，未知1" in field.text for field in heartbeat.fields)
        assert not any("缺口" in field.text or field.text.startswith(("说明 ", "错误 ")) for field in heartbeat.fields)
        assert failure is None or all(failure not in field.text for field in heartbeat.fields)
    count = len(messages)
    progress.finish_owner("company")
    clock[0] = 40
    progress.heartbeat()
    assert len(messages) == count


def test_company_heartbeat_shows_live_page_and_keeps_source_counts_separate_from_the_ledger():
    messages = []
    clock = [0.0]
    progress = SyncProgress(messages.append, lambda: clock[0])
    report = CompanyRoundProgress(progress, lambda: clock[0])
    report.start(4)
    round_id = "a" * 64
    report.observe_requests(CompanyRequestProgress(round_id, 20, 4, 0))
    family = CompanySyncFamily.FINANCIAL
    report.download(CompanyStructuredDownloadProgress(family, 2, 4, 1_000, 2_000), (round_id, family, 2))
    with report.activity(family):
        clock[0] = 1
        day = _ROUND_AT.date()
        report.download(CompanyDownloadOperation(family, "structured_page", StructuredCompanyQuery("financial", day, day, page=3)),
            (round_id, family, 3))
        report.observe_requests(CompanyRequestProgress(round_id, 20, 5, 1))
        clock[0] = 6
        progress.heartbeat()
    heartbeat = " | ".join(field.text for field in messages[-1].fields)
    assert "第3页 已用 00:00:05" in heartbeat and "东财财务 2/4（50.00%）页" in heartbeat
    assert "距最近入库 00:00:06" in heartbeat and "本次新增1次" in heartbeat
    assert "轮次累计5/20次" in heartbeat and "未确认1次" in heartbeat


def test_company_finishes_with_measured_execution_and_pinned_field_counts():
    messages = []
    clock = [0.0]
    progress = SyncProgress(messages.append, lambda: clock[0])
    company_progress = CompanyRoundProgress(progress, lambda: clock[0])
    company_progress.start(4403)
    pledge = CompanySyncFamilyFixture(CompanySyncFamily.PLEDGE, gaps=("pledge_population_coverage_unverified",))
    announcements = replace(
        CompanySyncFamilyFixture(
            CompanySyncFamily.ANNOUNCEMENTS, failure="source_attempt_unconfirmed", source_calls=1,
        ),
        unknown_attempts=1,
    )
    news = CompanySyncFamilyFixture(CompanySyncFamily.NEWS, gaps=("news_metadata_does_not_verify_issuer_risk",))
    field = CompanySyncFamilyResult(
        CompanySyncFamily.FIELDS, 371, 1_633_513, 0,
        CompanySyncStepResult(
            True, False, 4403, changed_codes=("600001", "600002"), pending_count=47, gaps=("field_proof_incomplete",),
        ),
    )
    with company_progress.activity(CompanySyncFamily.FIELDS):
        clock[0] = 3
        company_progress.advance(field)
    clock[0] = 30
    with company_progress.activity(CompanySyncFamily.FIELDS):
        clock[0] = 34
        company_progress.advance(field)
    round_result = CompanySyncRoundFixture(_ROUND_AT, families=(pledge, announcements, news, field))
    company_progress.finish(round_result, fields=CompanyFieldOutcomeCounts(
        publication_hash=round_result.publication_hash,
        verified=25,
        present_unverified=37,
        other_pending=10,
        not_disclosed=0,
        not_applicable=46,
        pending_documents=85,
    ))
    terminal = {message.task.key: message for message in messages if message.finished}
    pledge_done, announcements_done, news_done, fields_done = (
        terminal[family.value] for family in (CompanySyncFamily.PLEDGE, CompanySyncFamily.ANNOUNCEMENTS,
            CompanySyncFamily.NEWS, CompanySyncFamily.FIELDS)
    )
    assert pledge_done.state == "待补证" and "--:--:--" in pledge_done.fields[1].text
    assert any("本次未执行，沿用账本" in item.text for item in pledge_done.fields)
    assert announcements_done.state == "阻塞" and any("source_attempt_unconfirmed" in item.text for item in announcements_done.fields)
    assert news_done.state == "采集结束" and any("新闻用于辅助研究" in item.text for item in news_done.fields)
    assert fields_done.state == "待补证" and "00:00:07" in fields_done.fields[1].text
    text = " | ".join(item.text for item in fields_done.fields)
    assert "累计核验1633513股次/371步，累计变动2股" in text
    assert "本次确认2步，执行用时7.00秒" in text and "本次最近确认核验4403股" in text
    assert "正式字段核实25项，有值待核37项，其他待核10项，已证未披露0项，不适用46项" in text
    assert "各股待处理正文合计85篇次" in text
    count = len(messages)
    clock[0] = 40
    progress.heartbeat()
    company_progress.advance(field)
    assert len(messages) == count


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
def test_sync_failures_explain_recovery_and_end_with_a_readable_summary(monkeypatch, capsys, result, expected):
    monkeypatch.setattr(bootstrap, "ExecuteSync", lambda *args, **kwargs: result)
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 1
    captured = capsys.readouterr()
    assert captured.out.startswith("公司同步：失败\n原因：") and "\"status\"" not in captured.out
    assert expected in captured.err


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


def test_terminal_and_fill_summary_show_pending_units_without_claiming_full_coverage(monkeypatch, capsys):
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
    result = CompanySyncRoundFixture(
        datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")),
        tuple(f"{code:06d}" for code in range(4402)),
        families=tuple(CompanySyncFamilyFixture(
            family, complete=False, can_continue=True,
            pending_count=26 if family is CompanySyncFamily.ANNOUNCEMENTS else 3,
            failure="timeout" if family is CompanySyncFamily.ANNOUNCEMENTS else None,
        ) if family in {CompanySyncFamily.ANNOUNCEMENTS, CompanySyncFamily.FIELDS}
            else CompanySyncFamilyFixture(family) for family in CompanySyncFamily),
    )
    def execute(*args, **kwargs):
        assert kwargs["fill_missing"]
        return SyncResult((SyncTaskResult("company", "pending", company=result),))

    monkeypatch.setattr(bootstrap, "ExecuteSync", execute)
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company", "fill"]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "公司同步：待继续，范围 4,402 股",
        "来源库：财务采集处理结束、质押采集处理结束、解禁采集处理结束、公告有异常、年度营收采集处理结束。",
        "正式库：已确认发布，业务资料仍有缺口。", "异常：公告：请求超时。",
        "待处理：公告 26 项、字段核验 3 项。", "请求账本：已占用 0 次，未确认 0 次。",
    ]


def test_cancelled_terminal_and_summary_report_unknown_counts_without_claiming_zero_pending(monkeypatch, capsys):
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
    result = CompanySyncRoundFixture(
        datetime(2026, 10, 18, tzinfo=ZoneInfo("Asia/Shanghai")), ("600001", "600002"),
        families=(replace(CompanySyncFamilyFixture(
            CompanySyncFamily.FINANCIAL, complete=False, pending_count=None, accepted_inputs=1, source_calls=None,
        ), unknown_attempts=None),), cancelled=True,
    )
    assert result.needs_retry and replace(result, cancelled=False).needs_retry
    monkeypatch.setattr(
        bootstrap, "ExecuteSync",
        lambda *args, **kwargs: SyncResult((SyncTaskResult("company", "cancelled", company=result),), cancelled=True),
    )
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 130
    assert capsys.readouterr().out.splitlines() == [
        "公司同步：取消，范围 2 股",
        "来源库：财务采集待核、质押未核对、解禁未核对、公告未核对、年度营收未核对。",
        "正式库：已确认发布，业务资料仍有缺口。",
        "请求账本：本轮总数未核对。",
    ]


def test_last_stock_closes_verification_without_claiming_a_publication():
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0)
    ReportCompanyStock(report, (("600001", "示例"),), CompanyStockProgress("600001", processed=1, total=1))
    assert "正式提交" not in stream.getvalue()
    ReportCompanyStock(report, (), CompanyPublicationProgress("started", ("600001",)))
    ReportCompanyStock(report, (), CompanyPublicationProgress("committed", ("600001",), ("600001",)))
    report(EvidenceSyncProgress("publish", 1, 0, 2, 3, 0, status="complete"))
    report(EvidenceSyncProgress("news", 0, 0, 2, 3, 0, status="complete"))
    facts, pending, stock_result, published, finished, news = _messages(stream.getvalue())
    assert "来源本地" in facts and "600001-示例" in facts
    assert "正式提交中，尚未发布" in pending
    assert "已提交，部分覆盖" in stock_result and "正式提交回执 已确认" in stock_result
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
