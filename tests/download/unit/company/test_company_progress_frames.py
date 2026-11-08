"""Three-line company snapshots share one time and one terminal write boundary."""

import re
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from threading import Barrier, Event, Lock

import pytest

from tests.helpers.download.company.company_evidence import _disclosure
from trader.download.application.company_operations import CompanyOperations
from trader.download.entrypoints.sync_progress import SyncProgress
from trader.download.application.progress_message import (
    ProgressField,
    ProgressMessage,
    ProgressRow,
    ProgressTask,
)
from trader.download.infra.progress_projection import WithProgressTime
from trader.download.domain.company_evidence import (
    CompanyOperationKind, CompanyOperationResult, CompanyStockProgress, EvidenceSyncProgress,
)
from trader.download.entrypoints.company_evidence_progress import CompanyEvidenceProgress, ReportCompanyStock
from trader.download.infra.terminal_progress import RenderProgress, TerminalDownloadProgress


class _RecordingStream(StringIO):
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


def _frame(marker: str, *, compact: bool = False) -> ProgressMessage:
    return ProgressMessage(
        (ProgressField(marker + " 总览"),),
        task=ProgressTask("company", "financial", "总览"),
        compact=compact,
        rows=(
            ProgressRow("范围", (ProgressField(marker + " 范围"),)),
            ProgressRow("任务", (ProgressField(marker + " 任务"),)),
        ),
    )


def _frame_text(frame: ProgressMessage) -> str:
    return " | ".join(field.text for fields in (frame.fields, *(row.fields for row in frame.rows)) for field in fields)


def test_three_line_frame_writes_and_flushes_once_and_cleans_field_newlines() -> None:
    stream = _RecordingStream()
    frame = _frame("巨潮\n公告\x1b[31m|控制符")
    message = WithProgressTime(frame, 590.0, 123.0)
    TerminalDownloadProgress(stream)(message)
    assert len(stream.writes) == stream.flushes == 1
    assert stream.writes[0] == RenderProgress(message) + "\n"
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3
    assert all("\x1b" not in line and "巨潮 公告 控制符" in line for line in lines)


@pytest.mark.parametrize("compact", [False, True])
def test_all_three_prefixes_share_total_and_stage_time(compact: bool) -> None:
    frame = WithProgressTime(_frame("同次快照", compact=compact), 590.9, 123.9)
    fields = (frame.fields, *(row.fields for row in frame.rows))
    assert [row[0].text.strip() for row in fields] == ["00:09:50"] * 3
    assert all(row[1].text.rstrip().endswith("00:02:03") for row in fields)
    assert [row[1].text.split()[0] for row in fields] == ["company·总览", "company·范围", "company·任务"]
    assert len(RenderProgress(frame).splitlines()) == 3


def test_two_concurrent_frames_do_not_interleave_writes_or_flushes() -> None:
    class ConcurrentStream(StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[str, str]] = []
            self.lock = Lock()
            self.second_flushed = Event()
            self.calls = 0

        def write(self, value: str) -> int:
            with self.lock:
                self.calls += 1
                first = self.calls == 1
                self.events.append(("write", value))
            if first:
                # Give a second caller a chance to expose a missing terminal lock.
                self.second_flushed.wait(0.05)
            return super().write(value)

        def flush(self) -> None:
            with self.lock:
                self.events.append(("flush", ""))
                if self.calls == 2:
                    self.second_flushed.set()
            super().flush()

    stream = ConcurrentStream()
    terminal = TerminalDownloadProgress(stream)
    ready = Barrier(2)

    def emit(marker: str) -> None:
        ready.wait(timeout=5)
        terminal(WithProgressTime(_frame(marker), 5.0, 5.0))

    with ThreadPoolExecutor(max_workers=2) as workers:
        jobs = (workers.submit(emit, "AAA"), workers.submit(emit, "BBB"))
        for job in jobs:
            job.result(timeout=5)
    assert [kind for kind, _ in stream.events] == ["write", "flush", "write", "flush"]
    chunks = [value for kind, value in stream.events if kind == "write"]
    assert all(len(chunk.splitlines()) == 3 for chunk in chunks)
    assert {"AAA" if "AAA" in chunk else "BBB" for chunk in chunks} == {"AAA", "BBB"}
    assert all(not ("AAA" in chunk and "BBB" in chunk) for chunk in chunks)
    assert stream.getvalue() == "".join(chunks)


def test_company_snapshot_has_overview_scope_and_tasks_without_promoting_old_coverage() -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    report(EvidenceSyncProgress("resume", 0, None, 0, 0, 0, population=4402,
                                existing_published_codes=4402, sources=("local",)))
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0, sources=("eastmoney",)))
    frame = messages[-1]
    assert frame.task is not None and frame.task.label == "总览"
    assert tuple(row.label for row in frame.rows) == ("范围", "任务")
    text = _frame_text(frame)
    assert "财务已保存0/待发现页" in text and "来源东财" in text
    assert not frame.finished and frame.state != "完成"
    assert "100.00%" not in text and "4402/4402" not in text.replace(" ", "")
    assert re.search(r"本轮(?:已核验|已处理|已发布|完成|核验|发布)?\s*4402", text) is None


def test_worker_page_updates_only_observe_and_snapshot_shows_real_ocr_pages_and_sources() -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress("risk_documents", 0, None, 0, 0, 0, sources=("cninfo",)))
    baseline = len(messages)
    operations = CompanyOperations(report, lambda: clock[0], (("600001", "示例"),))
    event = operations.queued("ocr", document=_disclosure())
    with operations.running(event) as pages:
        pages(3, 12)
        assert len(messages) == baseline
        clock[0] = 5.0
        frame = report.heartbeat_message()
        assert frame is not None and frame.task is not None and frame.task.label == "总览"
        assert tuple(row.label for row in frame.rows) == ("范围", "任务")
        text = _frame_text(frame)
        assert "OCR" in text and "巨潮" in text
        assert re.search(r"3\s*/\s*12", text) is not None and "页" in text
        assert "600001" in text
    assert len(messages) == baseline
    clock[0] = 10.0
    ended = report.heartbeat_message()
    assert ended is not None and re.search(r"3\s*/\s*12", _frame_text(ended)) is None


@pytest.mark.parametrize("explicit_preparation", [False, True])
def test_completed_restore_is_counted_before_the_next_financial_snapshot(explicit_preparation: bool) -> None:
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: 0.0)
    if explicit_preparation:
        report(EvidenceSyncProgress("prepare", 1, 0, 0, 0, 0, status="complete", sources=("local",)))
    report(EvidenceSyncProgress("resume", 0, None, 0, 0, 0,
                                status="complete" if explicit_preparation else "running", sources=("local",)))
    report(EvidenceSyncProgress("financial", 0, None, 0, 0, 0, sources=("eastmoney",)))
    assert messages[-1].fields[0].text == "事项9项：完成1、进行1、待处理7、待续传0"
    assert "财务已保存0/待发现页" in _frame_text(messages[-1])


def test_row_only_change_refreshes_the_generic_heartbeat_fact_age() -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    progress = SyncProgress(messages.append, lambda: clock[0])
    original = _frame("主行不变")
    progress(original)
    clock[0] = 2.0
    changed = replace(original, rows=(original.rows[0], ProgressRow("任务", (ProgressField("OCR已处理3/12页"),))))
    progress(changed)
    clock[0] = 7.0
    progress.heartbeat()
    text = _frame_text(messages[-1])
    assert "距该阶段事实更新 00:00:05" in text
    assert "距该阶段事实更新 00:00:07" not in text
    assert "OCR已处理3/12页" in text


@pytest.mark.parametrize(("component", "commit_kind"), [("annual_income", "collection_commit"), ("news", "collection_commit")])
def test_confirmed_business_results_refresh_persistence_age_without_worker_output(
    component: str, commit_kind: CompanyOperationKind,
) -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    report(EvidenceSyncProgress(component, 0, 1, 0, 0, 0, sources=("eastmoney",)))
    count = len(messages)
    operations = CompanyOperations(report, lambda: clock[0], ())
    clock[0] = 5.0
    operations.call(commit_kind, lambda: None)
    assert len(messages) == count
    report(EvidenceSyncProgress("result", 0, None, 0, 0, 0, result=CompanyOperationResult(
        1, "annual_income" if component == "annual_income" else "news", "eastmoney", component, "accepted",
        component=component, commit_operation_id=2,
    )))
    clock[0] = 6.0
    pending = operations.queued(commit_kind)
    clock[0] = 7.0
    with pytest.raises(RuntimeError, match="fixture_commit_failed"), operations.running(pending):
        raise RuntimeError("fixture_commit_failed")
    clock[0] = 8.0
    operations.call("inventory", lambda: None)
    clock[0] = 9.0
    operations.call("publication", lambda: None)
    assert len(messages) == count + 1
    clock[0] = 10.0
    snapshot = report.heartbeat_message()
    assert snapshot is not None and "距持久推进00:00:05" in _frame_text(snapshot)
    ReportCompanyStock(report, (), CompanyStockProgress("600001", processed=1, total=2))
    clock[0] = 15.0
    verified = report.heartbeat_message()
    assert verified is not None
    assert "距持久推进00:00:10" in _frame_text(verified)
    assert "核验未发布" in _frame_text(verified)


def test_superseded_document_commit_does_not_claim_received_or_persisted_progress() -> None:
    clock = [0.0]
    messages: list[ProgressMessage] = []
    report = CompanyEvidenceProgress(report=messages.append, monotonic=lambda: clock[0])
    original = EvidenceSyncProgress("risk_documents", 0, 1, 0, 0, 0, sources=("cninfo",), population=1)
    report(original)
    operations = CompanyOperations(report, lambda: clock[0], (("600001", "示例"),), population=("600001",))
    clock[0] = 5.0
    assert operations.call("document_commit", lambda: False, document=_disclosure()) is False
    assert len(messages) == 1
    clock[0] = 10.0
    superseded = report.heartbeat_message()
    assert superseded is not None
    text = _frame_text(superseded)
    assert "本轮已获取0股" in text and "本轮尚无新的持久推进" in text
    assert "正文已入库0篇" in text
    clock[0] = 15.0
    report(replace(original, completed=1, pending=0))
    clock[0] = 20.0
    accepted = report.heartbeat_message()
    assert accepted is not None
    assert "正文已入库1篇" in _frame_text(accepted)
    assert "距持久推进00:00:05" in _frame_text(accepted)
