"""Producer-confirmed company results are immediate, bounded terminal evidence."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from threading import Barrier, Event, Lock
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.company.company_evidence import _disclosure
from trader import bootstrap
from trader.download.application.progress_message import ProgressMessage
from trader.download.application.sync_data import SyncResult
from trader.download.domain.company_evidence import (
    CompanyOperationResult,
    CompanyDateProgress,
    CompanyOperationProgress,
    CompanyPublicationProgress,
    CompanyRecoveryDecision,
    CompanyStockProgress,
    EvidenceSyncProgress,
    DisclosurePartition,
    StructuredCompanyQuery,
)
from trader.download.domain.download_failure import (
    DownloadFailureContext,
    DownloadFailureObservation,
    DownloadFailureRecord,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, ReportCompanyStock
from trader.download.infra.download_failure_diagnostics import DownloadFailureDiagnostics
from trader.download.infra.terminal_progress import RenderProgress
from trader.entrypoints.cli import Main

_NOW = datetime(2026, 11, 7, 12, tzinfo=ZoneInfo("Asia/Shanghai"))


class _Stream(StringIO):
    def __init__(self) -> None:
        super().__init__()
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, value: str) -> int:
        self.writes.append(value)
        return super().write(value)

    def flush(self) -> None:
        self.flushes += 1
        super().flush()


def _result(operation_id: int = 1, object_id: str = "cninfo:document-a") -> CompanyOperationResult:
    document = _disclosure(object_id.split(":")[-1], "结果示例公告")
    return CompanyOperationResult(
        operation_id, "document_commit", "cninfo", object_id, "accepted", run_id="fixture-run",
        code="600001", name="示例", metadata=document, revision=document.digest,
        related_codes=("600001",), queued_seconds=0.25, call_seconds=2.5, commit_seconds=0.75,
        commit_operation_id=operation_id + 100, method="text", text_chars=1234,
    )


def _feedback(result: CompanyOperationResult) -> EvidenceSyncProgress:
    return EvidenceSyncProgress("operation", 0, None, 0, 0, 0, result=result)


def _diagnostic(operation_id: int = 10, object_id: str = "cninfo:document-a") -> DownloadFailureObservation:
    context = DownloadFailureContext(
        "document_commit", code="600001", supplier="cninfo", operation_id=operation_id,
        operation_kind="document_commit", object_id=object_id, revision="a",
        elapsed_seconds=2.5, queued_seconds=0.25, attempt=1, maximum_attempts=3,
        raw_saved=True, text_extracted=True, collection_committed=False, publication_committed=False,
    )
    return DownloadFailureObservation(DownloadFailureRecord(
        "fixture-run", _NOW, context, "company_commit_failed", "RuntimeError > OSError:errno_5",
        "RuntimeError:test_company_result_feedback.py:64 > OSError:errno_5:test_company_result_feedback.py:62",
    ))


def test_page_and_document_results_are_immediate_without_postponing_the_snapshot() -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0, sources=("eastmoney",)))
    query = StructuredCompanyQuery("financial", date(2026, 1, 1), date(2026, 11, 7), page=3)
    clock[0] = 1.0
    page = CompanyOperationResult(
        1, "structured", "eastmoney", "financial:2026-01-01:2026-11-07:3", "accepted",
        run_id="fixture-run", scope=query, rows=30, call_seconds=1.25,
        commit_operation_id=2, commit_seconds=0.5,
    )
    report(_feedback(page))
    clock[0] = 2.0
    report(_feedback(replace(_result(3), kind="download", outcome="downloaded", raw_bytes=2048)))
    clock[0] = 3.0
    report(_feedback(_result(4)))
    assert len(messages) == 4
    assert all(message.immediate for message in messages[1:])
    assert "30" in RenderProgress(messages[1]) and "东财" in RenderProgress(messages[1])
    assert "结果示例公告" in RenderProgress(messages[2]) and "2048" in RenderProgress(messages[2])
    assert "1234" in RenderProgress(messages[3])
    clock[0] = 5.0
    snapshot = report.heartbeat_message()
    assert snapshot is not None and snapshot.task.label == "总览"


@pytest.mark.parametrize(("outcome", "words"), [
    ("empty", ("空", "无数据", "无记录")), ("reused", ("复用",)), ("rejected", ("拒", "未接纳", "旧修订")),
])
def test_empty_reused_and_rejected_results_are_distinct_from_acceptance(outcome, words) -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    result = replace(_result(), outcome=outcome)
    report(_feedback(result))
    assert len(messages) == 1 and messages[0].immediate
    text = RenderProgress(messages[0])
    assert any(word in text for word in words)
    assert result.object_id in text
    report(_feedback(result))
    assert len(messages) == 1


def test_formal_receipts_are_per_stock_and_repeated_operations_are_deduplicated() -> None:
    stream = _Stream()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0.0)
    names = (("600001", "示例"), ("600002", "另一股"))
    for code, _name in names:
        ReportCompanyStock(report, names, CompanyStockProgress(code))
    receipt = CompanyPublicationProgress("committed", ("600001", "600002"), ("600001", "600002"),
                                         publication_id="fixture-publication-1")
    before = len(stream.writes)
    ReportCompanyStock(report, names, receipt)
    committed = [chunk for chunk in stream.writes[before:] if "正式提交回执" in chunk]
    assert len(committed) == 2
    assert "600001" in committed[0] and "600002" in committed[1]
    count = len(committed)
    ReportCompanyStock(report, names, receipt)
    assert len([chunk for chunk in stream.writes[before:] if "正式提交回执" in chunk]) == count
    ReportCompanyStock(report, names, replace(receipt, publication_id="fixture-publication-2"))
    assert len([chunk for chunk in stream.writes[before:] if "正式提交回执" in chunk]) == count + 2
    assert len(stream.writes) == stream.flushes


def test_same_reason_on_different_objects_is_not_lost_and_duplicate_observation_is_quiet() -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    first = _diagnostic()
    second = _diagnostic(11, "cninfo:document-b")
    for observation in (first, second, first):
        report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=observation))
    assert len(messages) == 2
    for message, observation in zip(messages, (first, second), strict=True):
        text = RenderProgress(message)
        assert message.immediate and observation.record.context.object_id in text
        assert observation.record.run_id in text
        assert str(observation.record.context.operation_id) in text
        assert "errno_5" in text and "test_company_result_feedback.py" in text
        assert "保存" in text and ("恢复" in text or "重试" in text or "后续" in text)
        assert "排队0.250秒" in text.replace(" ", "") and "调用2.500秒" in text.replace(" ", "")
        assert "原件保存 已确认" in text and "正文提取 已确认" in text
        assert "资料提交 未发生" in text and "正式发布 未发生" in text


def test_safe_diagnostic_evidence_does_not_expose_exception_messages_or_paths() -> None:
    stream = _Stream()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 100.0)
    observed: list[DownloadFailureObservation] = []

    def observe(value: DownloadFailureObservation) -> None:
        observed.append(value)
        report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=value))

    recorder = DownloadFailureDiagnostics(lambda record: None, lambda: _NOW, observe=observe)
    try:
        try:
            raise OSError(5, "private-token=secret", "/private/full/document.pdf")
        except OSError as cause:
            raise RuntimeError("remote-payload=https://private.example/api?token=secret") from cause
    except RuntimeError as failure:
        recorder(_diagnostic().record.context, failure)
    assert {value.persistence for value in observed} == {"pending", "saved"}
    text = stream.getvalue()
    assert recorder.run_id in text and "errno_5" in text and "test_company_result_feedback.py" in text
    assert "private-token" not in text and "private.example" not in text and "/private/full" not in text
    assert "2.5" in text or "00:00:02" in text
    assert "0.25" in text or "00:00:00" in text
    assert len(stream.writes) == stream.flushes


def test_retry_decision_keeps_failure_open_until_a_new_operation_succeeds() -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("risk_documents", 0, 2, 0, 0, 0, sources=("cninfo",)))
    first, second = _diagnostic(), _diagnostic(11, "cninfo:document-b")
    for observation in (first, second):
        report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=observation))
    clock[0] = 5.0
    before = report.heartbeat_message()
    assert before is not None and "本轮仍未恢复2个对象" in RenderProgress(before).replace(" ", "")
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, recovery=CompanyRecoveryDecision(
        10, "document_commit", "cninfo", first.record.context.object_id, "retry", "company_commit_failed",
        run_id="fixture-run", revision="a",
    )))
    clock[0] = 7.0
    report(_feedback(_result(12)))
    assert any("关联错误操作 10" in RenderProgress(message) for message in messages)
    clock[0] = 10.0
    recovered = report.heartbeat_message()
    assert recovered is not None
    summary = RenderProgress(recovered).replace(" ", "")
    assert "本轮错误2次" in summary and "已恢复1个对象" in summary and "本轮仍未恢复1个对象" in summary


@pytest.mark.parametrize("kind", ["download", "inventory"])
def test_raw_file_reuse_does_not_resolve_unfinished_ocr_or_commit_failure(kind) -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("risk_documents", 0, 1, 0, 0, 0, sources=("cninfo",)))
    failure = _diagnostic()
    failure = replace(failure, record=replace(failure.record, context=replace(
        failure.record.context, stage="company_ocr", operation_kind="ocr", text_extracted=False,
    )))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=failure))
    before = len(messages)
    report(_feedback(replace(_result(11), kind=kind, outcome="reused")))
    assert not any("恢复成功" in RenderProgress(message) for message in messages[before:])
    clock[0] = 5.0
    snapshot = report.heartbeat_message()
    assert snapshot is not None
    summary = RenderProgress(snapshot).replace(" ", "")
    assert "已恢复0个对象" in summary and "本轮仍未恢复1个对象" in summary


def test_diagnostic_persistence_failure_keeps_the_original_error_and_is_immediate() -> None:
    stream = _Stream()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0.0)

    def persist(_record: DownloadFailureRecord) -> None:
        raise OSError(28, "untrusted-full-disk-path", "/private/store.db")

    recorder = DownloadFailureDiagnostics(persist, lambda: _NOW, observe=lambda observation: report(
        EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=observation),
    ))
    recorder(_diagnostic().record.context, RuntimeError("company_commit_failed"))
    assert len(stream.writes) == stream.flushes == 2
    assert "company_commit_failed" in stream.writes[0]
    assert "诊断保存失败" in stream.writes[1] and "errno_28" in stream.writes[1]
    assert "原始错误保留" in stream.writes[1]
    assert "untrusted-full-disk-path" not in stream.getvalue() and "/private/store.db" not in stream.getvalue()


@pytest.mark.parametrize("failure", [False, True])
def test_concurrent_result_blocks_are_written_atomically(failure: bool) -> None:
    stream = _Stream()
    report = CompanyEvidenceProgress(stream, monotonic=lambda: 0.0)
    ready = Barrier(2)

    def emit(number: int) -> None:
        ready.wait(timeout=5)
        if failure:
            report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0,
                                        diagnostic=_diagnostic(number, f"cninfo:document-{number}")))
        else:
            report(_feedback(_result(number, f"cninfo:document-{number}")))

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = (workers.submit(emit, 1), workers.submit(emit, 2))
        for job in jobs:
            job.result(timeout=5)
    assert len(stream.writes) == stream.flushes == 2
    assert all(not ("cninfo:document-1" in chunk and "cninfo:document-2" in chunk) for chunk in stream.writes)
    if failure:
        assert all(len(chunk.splitlines()) > 1 for chunk in stream.writes)


def test_cancel_fences_in_flight_and_late_feedback_blocks() -> None:
    messages: list[ProgressMessage] = []
    entered, release, terminal_reported = Event(), Event(), Event()
    first = True
    lock = Lock()

    def capture(message: ProgressMessage) -> None:
        nonlocal first
        with lock:
            block, first = first, False
        if block:
            entered.set()
            assert release.wait(5)
            terminal_reported.wait(0.05)
        if message.owner_finished:
            terminal_reported.set()
        messages.append(message)

    report = CompanyEvidenceProgress(report=capture, monotonic=lambda: 0.0)
    cancelled = EvidenceSyncProgress("complete", 0, None, 0, 0, 0, "task:cancelled", status="deferred")
    with ThreadPoolExecutor(max_workers=2) as workers:
        result = workers.submit(report, _feedback(_result()))
        try:
            assert entered.wait(5)
            terminal = workers.submit(report, cancelled)
        finally:
            release.set()
        result.result(timeout=5)
        terminal.result(timeout=5)
    owner_done = next(index for index, message in enumerate(messages) if message.owner_finished)
    assert owner_done > 0
    count = len(messages)
    report(_feedback(_result(2)))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=_diagnostic()))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0,
                                recovery=CompanyRecoveryDecision(10, "document_commit", "cninfo", "doc", "retry", "retry")))
    assert len(messages) == count


def test_feedback_is_stderr_only_and_cli_json_remains_machine_readable(monkeypatch, capsys) -> None:
    def execute(*args, **kwargs):
        CompanyEvidenceProgress(monotonic=lambda: 0.0)(_feedback(_result()))
        return SyncResult(())

    monkeypatch.setattr(bootstrap, "ExecuteSync", execute)
    config = Path(__file__).resolve().parents[4] / "config/runtime.json"
    assert Main(["--config", str(config), "sync", "company"]) == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema_version"] == "data_sync"
    assert "cninfo:document-a" in captured.err and "cninfo:document-a" not in captured.out


def test_replanned_parent_error_closes_only_when_all_directory_dates_are_confirmed() -> None:
    messages: list[ProgressMessage] = []
    clock = [0.0]
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    start, end = date(2026, 11, 6), date(2026, 11, 7)
    report(EvidenceSyncProgress("announcements", 0, 2, 0, 0, 0, dates=CompanyDateProgress(start, end, 0)))
    observation = _diagnostic(50, "sse:parent-window")
    context = replace(observation.record.context, component="announcements", operation_kind="collection_commit")
    report(EvidenceSyncProgress("diagnostic", 0, None, 0, 0, 0,
                                diagnostic=replace(observation, record=replace(observation.record, context=context))))
    report(_feedback(CompanyOperationResult(
        51, "directory", "cninfo", "sse:child-window", "accepted", run_id="fixture-run",
        scope=DisclosurePartition("sse", start, start), commit_operation_id=52, component="announcements",
    )))
    report(EvidenceSyncProgress("announcements", 1, 1, 1, 0, 0, status="complete",
                                dates=CompanyDateProgress(start, end, 1)))
    assert not any("本阶段确认完成" in RenderProgress(message) for message in messages)
    clock[0] = 10.0
    report(EvidenceSyncProgress("announcements", 2, 0, 2, 0, 0, status="complete",
                                dates=CompanyDateProgress(start, end, 2)))
    assert any("本阶段确认完成" in RenderProgress(message) for message in messages)
    assert "本轮仍未恢复0个对象" in RenderProgress(messages[-1]).replace(" ", "")


def test_unscoped_fatal_error_keeps_safe_evidence_without_inventing_an_object() -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    observation = _diagnostic()
    report(EvidenceSyncProgress("diagnostic", 0, None, 0, 0, 0, diagnostic=replace(
        observation, record=replace(observation.record, context=DownloadFailureContext("company_sync")),
    )))
    assert len(messages) == 1 and messages[0].task.key
    text = RenderProgress(messages[0])
    assert "errno_5" in text and "对象 未采集" in text and "股票 不适用" in text


@pytest.mark.parametrize(("component", "original_source"), [("pledge", "eastmoney"), ("news", "eastmoney_news")])
def test_cache_result_keeps_its_component_and_original_supplier(component, original_source) -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    result = CompanyOperationResult(
        20, "inventory", "local", component + ":cache", "reused", run_id="fixture-run",
        component=component, original_source=original_source,
    )
    report(_feedback(result))
    assert len(messages) == 1 and messages[0].task.key == component
    text = RenderProgress(messages[0])
    assert "本地复用" in text and "本地缓存" in text and "缓存原来源 东财" in text
    assert "恢复库存" not in text


@pytest.mark.parametrize(("component", "kind"), [("pledge", "structured"), ("news", "collection_commit")])
def test_failure_context_component_precedes_generic_operation_kind(component, kind) -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    observation = _diagnostic()
    observation = replace(observation, record=replace(observation.record, context=replace(
        observation.record.context, component=component, operation_kind=kind, stage="company_" + kind,
    )))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=observation))
    assert len(messages) == 1 and messages[0].task.key == component
    assert "company_" + kind in RenderProgress(messages[0])


def test_publication_failure_retains_receipts_confirmed_inside_the_incomplete_call() -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    report(_feedback(CompanyOperationResult(
        1, "inventory", "local", "run-start", "reused", run_id="fixture-run", component="resume",
    )))
    running = CompanyOperationProgress(50, "publication", "running", 0.0)
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=running))
    ReportCompanyStock(report, (), CompanyPublicationProgress(
        "committed", ("600001", "600002"), ("600001", "600002"), publication_id="actual-transaction-1",
    ))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, operation=replace(running, state="failed", at=4.0)))
    context = DownloadFailureContext(
        "company_publication", operation_id=50, operation_kind="publication", component="publish",
        object_id="publication-batch", supplier="local", elapsed_seconds=4.0,
    )
    observation = DownloadFailureObservation(DownloadFailureRecord(
        "fixture-run", _NOW, context, "company_publication_failed", "RuntimeError",
        "RuntimeError:test_company_result_feedback.py:1",
    ))
    report(EvidenceSyncProgress("operation", 0, None, 0, 0, 0, diagnostic=observation))
    failure = RenderProgress(messages[-1])
    assert "本操作内已确认正式提交 2股" in failure and "整次发布调用尚未确认完成" in failure
    report(EvidenceSyncProgress("complete", 0, None, 0, 0, 0, "task:RuntimeError", status="deferred",
                                pending_documents=None))
    summary = next(message for message in messages if message.task.key == "complete_summary")
    text = RenderProgress(summary)
    assert "本轮正式提交回执已确认 2 股" in text and "整库正式覆盖未重新核对" in text
    assert "已发布0股" not in text.replace(" ", "")


def test_true_component_completion_prints_one_confirmation_statistics_block() -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0, sources=("eastmoney",)))
    report(_feedback(CompanyOperationResult(
        1, "structured", "eastmoney", "financial:page-1", "accepted", run_id="fixture-run",
        component="financial", rows=10,
    )))
    end = EvidenceSyncProgress("financial", 1, 0, 1, 10, 0, status="complete", sources=("eastmoney",))
    report(end)
    report(end)
    confirmations = [message for message in messages if message.task.label == "财务结果"]
    assert len(confirmations) == 1
    text = RenderProgress(confirmations[0])
    assert "阶段确认结果" in text and "接纳 1次" in text
    assert "业务结果" in text and "不代表物理HTTP次数" in text


@pytest.mark.parametrize(("failure", "completed", "pending", "pending_documents", "state", "word"), [
    ("", 1, 2, 1, "等待", "已发布但待补"),
    ("task:RuntimeError", 0, None, None, "失败", "失败，已确认资料保留"),
    ("task:cancelled", 0, None, None, "取消", "取消，待处理数量未核对"),
])
def test_terminal_blocks_distinguish_pending_failed_and_cancelled(
    failure, completed, pending, pending_documents, state, word,
) -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    report(EvidenceSyncProgress("complete", completed, pending, 0, 0, 0, failure, status="deferred",
                                pending_documents=pending_documents))
    terminal = [message for message in messages if message.task.key in {"complete", "complete_summary"}]
    assert len(terminal) == 2 and all(message.state == state for message in terminal)
    assert word in RenderProgress(terminal[0])
