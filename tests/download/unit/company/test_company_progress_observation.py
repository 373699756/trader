"""Company snapshots observe real execution and committed publication boundaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from io import StringIO
from threading import Event, Thread
from zoneinfo import ZoneInfo

from trader.download.application.company_operations import CompanyOperations
from trader.download.application.sync_progress import SyncProgress
from trader.download.application.progress_message import WithProgressTime
from trader.download.domain.company_evidence import (
    CompanyOperationProgress,
    CompanyPublicationProgress,
    CompanyStockProgress,
    CompanyStructuredProgress,
    EvidenceSyncProgress,
    StructuredCompanyQuery,
)
from trader.download.domain.company_events import CompanyDataState
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, ReportCompanyStock
from trader.download.infra.terminal_progress import RenderProgress
from tests.helpers.download.company.company_evidence import _disclosure


def test_one_company_snapshot_every_five_seconds_combines_parallel_work():
    clock = [0.0]
    messages = []
    shared = SyncProgress(messages.append, lambda: clock[0])
    report = CompanyEvidenceProgress(report=shared, monotonic=lambda: clock[0])
    shared.register_observer("company", report.heartbeat_message)
    report(EvidenceSyncProgress("risk_documents", 1, 10, 0, 0, 0, unpublished_documents=1))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0,
                                operation=CompanyOperationProgress(1, "download", "running", 0, "600001", "示例", "公告甲")))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0,
                                operation=CompanyOperationProgress(2, "text", "running", 1, "600002", "另一股", "公告乙")))
    clock[0] = 2
    report(EvidenceSyncProgress("risk_documents", 2, 9, 0, 0, 0, unpublished_documents=2))
    shared.heartbeat()
    assert len(messages) == 1
    clock[0] = 5
    shared.heartbeat()
    assert len(messages) == 2
    line = RenderProgress(messages[-1])
    assert "下载1、文本1、OCR0" in line and "600001-示例" in line
    assert "00:00:05" in line and "HTTP" not in line
    assert "正文已入库2篇，可处理待补9篇" in line
    shared.heartbeat()
    assert len(messages) == 2


def test_worker_updates_observation_without_writing_and_queue_age_is_separate():
    clock = [0.0]
    stream = StringIO()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    operations = CompanyOperations(report, lambda: clock[0], (("600001", "示例"),))
    report(EvidenceSyncProgress("risk_documents", 0, 1, 0, 0, 0))
    first = stream.getvalue()
    blocker_started, release, executed = Event(), Event(), Event()

    def block():
        blocker_started.set()
        assert release.wait(5)

    def work():
        executed.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as executor:
        blocker = executor.submit(block)
        assert blocker_started.wait(5)
        pending = operations.submit("text", work, executor, document=_disclosure())
        clock[0] = 5
        queued = report.heartbeat_message()
        assert queued is not None
        text = RenderProgress(queued)
        assert "文本0" in text and "排队等待" in text
        assert not executed.is_set() and stream.getvalue() == first
        release.set()
        blocker.result(timeout=5)
        pending.result(timeout=5)
    assert executed.is_set() and stream.getvalue() == first
    clock[0] = 10
    ended = report.heartbeat_message()
    assert ended is not None
    assert all("最久" not in field.text for field in ended.fields)


def test_completed_inventory_and_verification_do_not_keep_old_heartbeats():
    clock = [0.0]
    report = CompanyEvidenceProgress(StringIO(), monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("resume", 0, None, 0, 0, 0))
    clock[0] = 5
    assert report.heartbeat_message() is None
    ReportCompanyStock(report, (), CompanyStockProgress("600001", processed=1, total=1))
    clock[0] = 10
    assert report.heartbeat_message() is None
    ReportCompanyStock(report, (), CompanyPublicationProgress("started", ("600001",)))
    clock[0] = 15
    assert report.heartbeat_message() is not None
    ReportCompanyStock(report, (), CompanyPublicationProgress("committed", ("600001",), ("600001",)))
    clock[0] = 20
    assert report.heartbeat_message() is not None
    report(EvidenceSyncProgress("publish", 1, 0, 0, 0, 0, status="complete", population=1))
    clock[0] = 25
    assert report.heartbeat_message() is None


def test_actual_publication_receipts_deduplicate_codes_and_failure_has_document_identity():
    stream = StringIO()
    clock = [0.0]
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    for _ in range(2):
        ReportCompanyStock(report, (), CompanyPublicationProgress("started", ("600001",)))
        ReportCompanyStock(report, (), CompanyPublicationProgress("committed", ("600001",), ("600001",)))
    report(EvidenceSyncProgress("complete", 1, 0, 0, 0, 0, population=1))
    assert "本轮变更股票 1 股（去重）" in stream.getvalue()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: clock[0])
    event = EvidenceSyncProgress("risk_documents", 0, 2, 0, 0, 0,
                                 "risk_documents:disclosure_document_unreadable", failed_documents=1,
                                 stock=CompanyStockProgress("600001", "示例"), failure_document=_disclosure(),
                                 failure_stage="text", failure_reason="disclosure_document_unreadable")
    report(event)
    clock[0] += 0.1
    report(replace(event, failed_documents=2, failure_document=_disclosure("second", "另一份公告")))
    failures = [line for line in stream.getvalue().splitlines() if "原因" in line]
    assert len(failures) == 2
    assert "600001 示例 普通公告" in failures[0] and "另一份公告" in failures[1]
    assert all("文本提取" in line and "PDF无法解析" in line for line in failures)
    assert "公告ID cninfo:first" in failures[0] and "公告ID cninfo:second" in failures[1]


def test_command_terminal_clears_operations_and_late_worker_events_cannot_reopen_it():
    clock = [0.0]
    report = CompanyEvidenceProgress(StringIO(), monotonic=lambda: clock[0])
    event = CompanyOperationProgress(1, "ocr", "running", 0, "600001", "示例", "公告")
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=event))
    report(EvidenceSyncProgress("risk_documents", 0, 1, 0, 0, 0))
    report(EvidenceSyncProgress("complete", 0, None, 0, 0, 0, "task:cancelled", status="deferred",
                                pending_documents=None, ocr_documents=None, unextractable_documents=None))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=event))
    clock[0] = 10
    assert report.heartbeat_message() is None


def test_snapshot_rows_keep_counts_names_sources_and_publication():
    messages = []
    clock = [0.0]
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0, sources=("eastmoney",)))
    report(EvidenceSyncProgress("financial", 12000, 12000, 0, 0, 0, status="complete", sources=("eastmoney",)))
    report(EvidenceSyncProgress("risk_documents", 1234567, 2, 0, 0, 0, sources=("cninfo",)))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=CompanyOperationProgress(
        1, "text", "running", 0, "600001", "很长的中文股票简称", "很长的中文公告标题用于验证显示宽度固定", "cninfo",
    )))
    clock[0] = 5
    messages.append(report.heartbeat_message())
    ReportCompanyStock(report, (("600001", "很长的中文股票简称"),), CompanyStockProgress("600001", processed=12000, total=12000))
    ReportCompanyStock(report, (), CompanyPublicationProgress("started", ("600001",)))
    ReportCompanyStock(report, (), CompanyPublicationProgress("committed", ("600001",), ("600001",)))
    assert all(message is not None for message in messages)
    assert all(message.task.label == "总览" and tuple(row.label for row in message.rows) == ("范围", "任务") for message in messages)
    assert any("600001" in RenderProgress(message) for message in messages)
    assert any("1234567" in RenderProgress(message) for message in messages)
    assert any("来源东财" in RenderProgress(message) and "财务已保存" in RenderProgress(message) for message in messages)


def test_batch_and_scope_updates_do_not_bypass_the_global_five_second_interval():
    clock = [0.0]
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("risk_documents", 0, 10, 0, 0, 0, batch=1, sources=("cninfo",)))
    clock[0] = 1
    report(EvidenceSyncProgress("risk_documents", 1, 9, 0, 0, 0, batch=2, sources=("cninfo",)))
    clock[0] = 4.9
    assert report.heartbeat_message() is None and len(messages) == 1
    clock[0] = 5
    message = report.heartbeat_message()
    assert message is not None and "正文已入库1篇" in RenderProgress(message)


def test_supplier_identity_follows_the_task_and_cache_keeps_its_original_source():
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0)
    operations = CompanyOperations(report, lambda: 0, (("600001", "宸展光电"),))
    task = operations.queued("text", document=_disclosure())
    assert task.source == "cninfo"
    report(EvidenceSyncProgress("financial", 0, 0, 0, 0, 1, status="reused", sources=("eastmoney",)))
    assert messages[-1].task.label == "总览" and "来源东财" in RenderProgress(messages[-1])
    assert "本地复用原来源缓存" in RenderProgress(messages[-1])
    report(EvidenceSyncProgress("news", 0, 0, 0, 0, 0, status="complete", sources=("eastmoney_news",)))
    assert "当前新闻；来源东财" in RenderProgress(messages[-1])
    report(EvidenceSyncProgress("risk_documents", 0, None, 0, 0, 0, sources=("cninfo", "eastmoney_news")))
    assert "来源巨潮/东财" in RenderProgress(messages[-1])
    assert "可处理待补未核对篇" in RenderProgress(messages[-1])
    assert "600001-宸展光电" in RenderProgress(messages[-1])


def test_terminal_rejects_an_already_constructed_heartbeat_snapshot():
    clock = [0.0]
    messages = []
    shared = SyncProgress(messages.append, lambda: clock[0])
    report = CompanyEvidenceProgress(report=shared, monotonic=lambda: clock[0])
    snapshot_ready, release = Event(), Event()

    def observer():
        snapshot = report.heartbeat_message()
        snapshot_ready.set()
        assert release.wait(5)
        return snapshot

    shared.register_observer("company", observer)
    report(EvidenceSyncProgress("risk_documents", 1, 2, 0, 0, 0, sources=("cninfo",)))
    clock[0] = 5
    thread = Thread(target=shared.heartbeat)
    thread.start()
    try:
        assert snapshot_ready.wait(5)
        report(EvidenceSyncProgress("complete", 1, 0, 0, 0, 0, population=1))
        count = len(messages)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert len(messages) == count
    assert any(message.owner_finished for message in messages)


def test_large_counters_remain_exact_in_three_row_snapshots():
    clock = [0.0]
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("risk_documents", 1, 1_234_567, 0, 0, 0, sources=("cninfo",)))
    assert "可处理待补1234567篇" in RenderProgress(messages[-1])
    clock[0] = 5
    report(EvidenceSyncProgress("risk_documents", 123_456_789_012, 1_234_567, 0, 0, 0,
                                unpublished_documents=123_456_789_012, sources=("cninfo",)))
    report(EvidenceSyncProgress("publish", 1_234_567, 7_890_123, 0, 0, 0,
                                population=9_000_000, sources=("local",)))
    report(EvidenceSyncProgress("financial", 1_234_567, 7_890_123, 0, 0, 0, sources=("eastmoney",)))
    assert all(tuple(row.label for row in message.rows) == ("范围", "任务") for message in messages)
    assert any("正文已入库123456789012篇" in RenderProgress(message) for message in messages)
    report(EvidenceSyncProgress("complete", 1_234_567, 7_890_123, 0, 0, 0, population=9_000_000))
    summary = " | ".join(field.text for field in messages[-1].fields)
    assert "1234567" in summary and "7890123" in summary


def test_committed_receipt_keeps_publication_cleanup_visible_until_index_returns():
    from trader.download.application.company_evidence import CompanyEvidenceDependencies, _SyncState, _publish_batch
    from tests.helpers.download.company import company_evidence as fixtures

    clock = [0.0]
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    committed, release = Event(), Event()

    class Index:
        def publish(self, codes, observed_at, *, changed_only=False):
            ReportCompanyStock(report, (), CompanyPublicationProgress("started", tuple(codes)))
            ReportCompanyStock(report, (), CompanyPublicationProgress("committed", tuple(codes), tuple(codes)))
            committed.set()
            assert release.wait(5)
            return len(codes)

        def published_count(self, codes):
            return len(codes)

        def annual_income_requests(self, codes, observed_at):
            return ()

    source = fixtures.Sources()
    dependencies = CompanyEvidenceDependencies(Index(), source, source, lambda: clock[0], report)
    state = _SyncState(CompanyOperations(report, lambda: clock[0], ()), 1)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_publish_batch, dependencies, ("600001",), fixtures.NOW, 0, state)
        try:
            assert committed.wait(5)
            clock[0] = 5
            snapshot = report.heartbeat_message()
            assert snapshot is not None
            line = RenderProgress(snapshot)
            assert "发布收尾（本批已提交）" in line and "尚未发布" not in line
            assert "本操作00:00:05" in line
        finally:
            release.set()
        assert future.result(timeout=5) == 1
    clock[0] = 10
    assert report.heartbeat_message() is None


def test_all_three_prefixes_keep_times_when_source_labels_are_long():
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0)
    for event in (
        EvidenceSyncProgress("prepare", 0, 1, 0, 0, 0, sources=("local",)),
        EvidenceSyncProgress("resume", 0, None, 0, 0, 0, status="complete", sources=("local",)),
        EvidenceSyncProgress("annual_income", 1, 2, 0, 0, 0, sources=("eastmoney",)),
        EvidenceSyncProgress("financial", 1, 2, 0, 0, 0, sources=("极长的真实供应商名称用于验证显示截短",)),
        EvidenceSyncProgress("complete", 1, 0, 0, 0, 0, population=1, sources=("local",)),
    ):
        report(event)
    rows = [WithProgressTime(message, 10, 2) for message in messages if not message.task.key.endswith("_summary")]
    assert len(rows) == 5
    assert all(len(RenderProgress(row).splitlines()) == 3 for row in rows)
    assert all(tuple(item.label for item in row.rows) == ("范围", "任务") for row in rows)
    assert all(all(fields[0].text.strip() == "00:00:10" and fields[1].text.rstrip().endswith("00:00:02")
                   for fields in (row.fields, *(item.fields for item in row.rows))) for row in rows)
    assert any("极长的真实供应商名称用于验证显示截短" in RenderProgress(row) for row in rows)


def test_resumed_and_reopened_queries_do_not_claim_old_pages_as_new_work():
    clock = [0.0]
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    query = StructuredCompanyQuery("financial", date(2026, 1, 1), date(2026, 11, 7), page=7)
    event = EvidenceSyncProgress(
        "financial", 7, None, 0, 0, 0, scope=query, population=4402, sources=("eastmoney",),
        structured=CompanyStructuredProgress(query, 700, resumed=True),
    )
    report(event)
    assert "本次续传新保存0页" in RenderProgress(messages[-1])
    assert "接续已保存分页" in RenderProgress(messages[-1])
    clock[0] = 5
    next_query = replace(query, page=8)
    report(replace(event, completed=8, scope=next_query,
                   structured=CompanyStructuredProgress(next_query, 800, page_rows=100)))
    continued = RenderProgress(messages[-1])
    assert "财务已保存8/待发现页" in continued and "本次续传新保存1页" in continued
    assert "当前查询累计800原始行" in continued
    clock[0] = 10
    reopened = replace(query, page=1)
    report(replace(event, completed=0, scope=reopened,
                   structured=CompanyStructuredProgress(reopened, 0, restarted=True)))
    restarted = RenderProgress(messages[-1])
    assert "查询重开，原页进度已清零" in restarted and "本次续传新保存0页" in restarted
    assert not messages[-1].finished and "100.0%" not in restarted


def test_stock_snapshot_keeps_data_states_and_partial_work_after_formal_commit():
    messages = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    stock = CompanyStockProgress(
        "600001", "示例", processed=1, total=4402, announcements=10, analyzed=4, pending=6,
        financial_state=CompanyDataState.COMPLETE, pledge_state=CompanyDataState.PARTIAL,
        unlock_state=CompanyDataState.UNAVAILABLE, announcement_state=CompanyDataState.PARTIAL,
        financial_report_date=date(2025, 12, 31),
        financial_disclosed_at=datetime(2026, 4, 19, tzinfo=ZoneInfo("Asia/Shanghai")),
        pledge_date=date(2026, 10, 31), unlock_coverage_end=date(2027, 5, 6),
    )
    ReportCompanyStock(report, (), stock)
    snapshot = RenderProgress(messages[-1])
    assert "财务完整、质押部分、解禁缺失、公告部分" in snapshot
    assert "该股正文已分析4/10篇，待补6篇" in snapshot
    assert "财务报告期2025-12-31，披露日2026-04-19" in snapshot
    assert "质押快照日2026-10-31" in snapshot and "解禁核对至2027-05-06" in snapshot
    assert "核验未发布" in snapshot and "已知待补1股" in snapshot
    ReportCompanyStock(report, (), CompanyPublicationProgress("started", ("600001",)))
    ReportCompanyStock(report, (), CompanyPublicationProgress("committed", ("600001",), ("600001",)))
    committed = RenderProgress(messages[-1])
    assert "提交1股（去重）" in committed and "已知待补1股" in committed
    assert "本批已提交" in committed and "100.0%" not in committed
