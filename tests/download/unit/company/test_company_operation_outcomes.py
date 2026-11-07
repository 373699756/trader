"""Caller decisions retain true operation identity, clocks, and primary causes."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event, get_ident
from typing import cast

import pytest

from tests.helpers.download.company.company_evidence import (
    NOW, Sources, _TEST_COMPANY_CONFIGURATION, _disclosure, _extraction, _sync_company_evidence,
    _index,
)
from trader.download.application.company_documents import CompanyDocumentRun
from trader.download.application.company_evidence import (
    CompanyEvidenceDependencies, CompanyEvidenceIndex, _AnnouncementRun, _CompanyRunContext, _SyncState,
)
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_structured import CollectCompanyStructured
from trader.download.domain.company_evidence import (
    CompanyDocumentFailure, CompanyDocumentTask, CompanyDocumentWorkload, CompanyFinancial,
    DisclosureCoverageMismatch, DisclosurePage, DisclosurePartition, StructuredCompanyPage,
)
from trader.infra.http_contracts import HttpCancelled


@pytest.mark.parametrize("accepted", (True, False))
def test_result_is_caller_confirmed_and_keeps_queue_call_commit_tokens(accepted):
    clock, events = [0.0], []
    item = replace(_disclosure(), codes=("600001", "600002"))
    operations = CompanyOperations(events.append, lambda: clock[0], (), population=item.codes, run_id="same-run")
    entered, release = Event(), Event()

    def blocker():
        entered.set()
        assert release.wait(5)

    def extract():
        clock[0] = 9.0
        return _extraction("正文" * 50)

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(blocker)
        assert entered.wait(5)
        future = operations.submit("text", extract, executor, document=item)
        clock[0] = 5.0
        release.set()
        waiting.result(timeout=5)
        extraction = future.result(timeout=5)
        timing = operations.take_future(future)
    assert not any(event.result is not None for event in events)
    clock[0] = 10.0

    def commit():
        clock[0] = 12.0
        return accepted

    value, committed = operations.call_observed("document_commit", commit, document=item)
    operations.result(timing, "accepted" if value else "rejected", commit=committed, extraction=extraction)
    result = events[-1].result
    assert result is not None and result.outcome == ("accepted" if accepted else "rejected")
    assert result.operation_id == timing.event.token and result.commit_operation_id == committed.event.token
    assert result.operation_id != result.commit_operation_id
    assert (result.queued_seconds, result.call_seconds, result.commit_seconds) == (5, 4, 2)
    assert result.metadata == item and result.revision == item.digest and result.related_codes == item.codes
    assert result.text_chars == len(extraction.text) and result.method == "text" and result.run_id == "same-run"


def test_unobserved_directory_failure_does_not_borrow_previous_fetch_timing():
    events, failures = [], []
    source = Sources()

    class Index:
        def partition_failure(self, *args):
            return None

        def defer_partition(self, *args):
            return None

    dependencies = CompanyEvidenceDependencies(
        cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0, events.append,
    )
    operations = CompanyOperations(events.append, lambda: 0.0, (),
                                   failures=lambda context, error: failures.append((context, error)))
    state = _SyncState(operations, 1)
    run = _AnnouncementRun(dependencies, state, _CompanyRunContext(NOW, 0, _TEST_COMPANY_CONFIGURATION, NOW.date()))
    partition = DisclosurePartition("sse", NOW.date(), NOW.date())
    _value, timing = operations.call_observed("directory", lambda: None, source="cninfo", scope=partition)
    run._fetch_timing = replace(timing, call_seconds=99.0)
    failure = sqlite3.OperationalError("untrusted local path")
    assert not run._failed(partition, failure)
    assert len(failures) == 1 and failures[0][1] is failure
    context = failures[0][0]
    assert context.operation_id is context.elapsed_seconds is None
    assert context.component == "announcements" and context.window_start == partition.start
    recovery = [event.recovery for event in events if event.recovery is not None][-1]
    assert recovery.object_id == context.object_id and recovery.source == context.supplier


def test_recovery_registration_failure_preserves_primary_cause_and_material_identity():
    events, failures = [], []
    item = _disclosure()
    operations = CompanyOperations(events.append, lambda: 0.0, (), failures=lambda context, exc: failures.append((context, exc)))
    cause, primary = OSError("socket failed"), RuntimeError("http_transport_failed")

    def fetch():
        raise primary from cause

    with pytest.raises(RuntimeError) as fetched:
        operations.call("download", fetch, document=item, component="risk_documents")

    def register():
        raise sqlite3.OperationalError("database locked")

    with pytest.raises(RuntimeError) as restored:
        operations.register_failure(fetched.value, register)
    assert restored.value is primary and primary.__cause__ is cause
    assert len(failures) == 2 and failures[0][1] is primary
    assert failures[0][0].operation_id != failures[1][0].operation_id
    assert (failures[0][0].supplier, failures[0][0].object_id, failures[0][0].revision) == (
        failures[1][0].supplier, failures[1][0].object_id, failures[1][0].revision,
    )
    assert events[-1].recovery.decision == "recording_failed"
    assert events[-1].recovery.secondary == "OperationalError"


def test_diagnostic_failure_does_not_replace_the_primary_exception():
    events = []

    def broken_diagnostics(context, exc):
        raise sqlite3.OperationalError("diagnostics unavailable")

    operations = CompanyOperations(events.append, lambda: 0.0, (), failures=broken_diagnostics)
    original = RuntimeError("http_transport_failed")

    def fail():
        raise original

    with pytest.raises(RuntimeError) as raised:
        operations.call("download", fail, document=_disclosure())
    assert raised.value is original and original.__cause__ is None
    assert events[-1].recovery.decision == "recording_failed"


@pytest.mark.parametrize("stage", ("prepare", "inventory"))
def test_fatal_prepare_reports_unknown_final_inventory_then_reraises(stage):
    events, failures = [], []
    original = sqlite3.OperationalError("prepare failed")

    class Index:
        def initialize(self):
            if stage == "prepare":
                raise original

        def prepare_publication(self, *args):
            return None

        def published_count(self, *args):
            return 0

        def document_workload(self, *args, **kwargs):
            raise original

    source = Sources()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0,
        events.append, failures=lambda context, exc: failures.append((context, exc)), run_id="fatal-run")
    with pytest.raises(sqlite3.OperationalError) as raised:
        _sync_company_evidence(dependencies, ("600001",), NOW)
    assert raised.value is original and len(failures) == 1
    terminal = events[-1]
    assert terminal.component == "complete" and terminal.status == "deferred" and "task:" in terminal.failure
    assert terminal.pending is terminal.pending_documents is terminal.ocr_documents is None
    assert terminal.run_id == "fatal-run" and terminal.completed == 0
    if stage == "inventory":
        assert failures[0][0].component == "risk_documents" and failures[0][0].operation_kind == "inventory"


@pytest.mark.parametrize("failure_type", (InterruptedError, HttpCancelled))
def test_cancelled_prepare_reports_cancelled_without_writing_diagnostics(failure_type):
    events, failures = [], []
    cancelled = Event()
    original = failure_type("cancelled")

    class Index:
        def initialize(self):
            cancelled.set()
            raise original

    source = Sources()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0,
        events.append, cancel_requested=cancelled.is_set,
        failures=lambda context, exc: failures.append((context, exc)), run_id="cancelled-run")
    with pytest.raises(failure_type) as raised:
        _sync_company_evidence(dependencies, ("600001",), NOW)
    assert raised.value is original and failures == []
    terminal = events[-1]
    assert terminal.component == "complete" and terminal.failure == "task:cancelled"
    assert terminal.status == "deferred" and terminal.run_id == "cancelled-run"
    assert terminal.pending is terminal.pending_documents is terminal.deferred_documents is None
    assert terminal.ocr_documents is terminal.unextractable_documents is None
    assert not any(event.diagnostic is not None for event in events)


@pytest.mark.parametrize("accepted", (True, False, "sql_error", "stage_sql", "ocr"))
def test_document_result_uses_real_acceptance_and_ocr_routing(accepted):
    events, failures, threads = [], [], []
    item = _disclosure()
    main_thread = get_ident()

    class Index:
        pending = True

        def document_workload(self, *args, **kwargs):
            return CompanyDocumentWorkload(int(self.pending), int(self.pending))

        def pending_documents(self, *args, **kwargs):
            return (CompanyDocumentTask(item, "standard"),) if self.pending else ()

        def document_required(self, *args):
            return self.pending

        def document_stage(self, document, stage):
            if stage == "downloaded":
                assert any(event.result is not None and event.result.outcome == "downloaded" for event in events)
                if accepted == "stage_sql":
                    raise sqlite3.OperationalError("stage failed")

        def commit_document(self, *args):
            if accepted == "sql_error":
                raise sqlite3.OperationalError("commit failed")
            self.pending = False
            return accepted is True or accepted == "ocr"

    class Source(Sources):
        def extract_document(self, document, *, route="standard", page_progress=None):
            if accepted == "ocr" and route == "standard":
                raise CompanyDocumentFailure("disclosure_document_ocr_required")
            if route == "ocr":
                page_progress(0, 2)
                page_progress(2, 2)
            return replace(_extraction("正文" * 50), method="ocr" if route == "ocr" else "text")

    def report(event):
        if event.result is not None:
            threads.append(get_ident())
        events.append(event)

    index, source = Index(), Source()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, index), source, source, lambda: 0.0, report)
    state = _SyncState(CompanyOperations(report, lambda: 0.0, (), population=("600001",),
                      failures=lambda context, exc: failures.append((context, exc))), 1)
    context = _CompanyRunContext(NOW, 0, _TEST_COMPANY_CONFIGURATION, NOW.date())
    documents = CompanyDocumentRun(dependencies, ("600001",), state, context, publish=lambda: pytest.fail("publication"))
    if accepted in ("sql_error", "stage_sql"):
        with pytest.raises(sqlite3.OperationalError):
            documents.run_batch()
        assert len(failures) == 1 and failures[0][0].raw_saved is True
        assert failures[0][0].text_extracted is (True if accepted == "sql_error" else None)
        if accepted == "stage_sql":
            assert failures[0][0].endpoint == "sqlite:document_stage.downloaded"
            assert failures[0][0].object_id == item.identity and failures[0][0].revision == item.digest
            assert failures[0][0].component == "risk_documents" and failures[0][0].operation_kind == "collection_commit"
    else:
        documents.run_batch()
    results = [event.result for event in events if event.result is not None]
    assert results[0].outcome == "downloaded" and results[0].metadata == item
    assert threads and set(threads) == {main_thread}
    if accepted in ("sql_error", "stage_sql"):
        assert len(results) == 1 and documents.completed == 0
    else:
        assert results[-1].outcome == ("rejected" if accepted is False else "accepted")
        assert documents.completed == int(accepted is True or accepted == "ocr")
        if accepted == "ocr":
            assert any(result.outcome == "ocr_queued" for result in results)
            assert any(event.recovery is not None and event.recovery.decision == "ocr_queued" for event in events)
            assert failures == [] and (results[-1].ocr_completed_pages, results[-1].ocr_total_pages) == (2, 2)


@pytest.mark.parametrize("mode", ("cache", "unchanged_network", "empty"))
def test_structured_zero_changes_do_not_claim_local_cache(mode):
    events = []

    class Index:
        def component_due(self, *args):
            return mode != "cache"

        def component_since(self, *args):
            return NOW.date() - timedelta(days=365)

        def structured_cursor(self, query):
            return query

        def commit_structured(self, *args):
            assert not any(event.result is not None for event in events)
            return 0

    class Source(Sources):
        def fetch(self, query, *, observed_at):
            assert mode != "cache"
            values = () if mode == "empty" else (CompanyFinancial(
                "600001", NOW.date(), NOW, 1, 2, 3, 4, 5, 6, 7, 8,
            ),)
            return StructuredCompanyPage("financial", 1, 1, len(values), financials=values)

    source = Source()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0.0, ()), 1)
    CollectCompanyStructured(dependencies, "financial", NOW, state)
    result = next(event.result for event in events if event.result is not None)
    assert result.component == "financial"
    assert result.outcome == {"cache": "reused", "unchanged_network": "accepted", "empty": "empty"}[mode]
    if mode == "cache":
        assert result.source == "local" and result.original_source == source.source
    else:
        assert result.changed_rows == 0 and result.original_source == ""


def test_structured_cursor_sql_failure_keeps_real_scope_and_primary():
    events, failures = [], []
    original = sqlite3.OperationalError("cursor unavailable")

    class Index:
        def component_due(self, *args):
            return True

        def component_since(self, *args):
            return NOW.date() - timedelta(days=365)

        def structured_cursor(self, query):
            raise original

    source = Sources()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0.0, (),
        failures=lambda context, exc: failures.append((context, exc))), 1)
    with pytest.raises(sqlite3.OperationalError) as raised:
        CollectCompanyStructured(dependencies, "financial", NOW, state)
    assert raised.value is original and len(failures) == 1
    context, error = failures[0]
    assert error is original and context.operation_id is not None and context.operation_kind == "inventory"
    assert context.component == "financial" and context.endpoint == "sqlite:company_structured_cursor"
    assert (context.window_start, context.window_end) == (NOW.date() - timedelta(days=365), NOW.date())
    assert not any(event.result is not None for event in events)


@pytest.mark.parametrize("changes", (None, 0))
def test_directory_refresh_reports_unchanged_without_claiming_commit(changes, monkeypatch):
    events = []
    partition = DisclosurePartition("sh", NOW.date(), NOW.date())

    class Index:
        def disclosure_queries(self, partition, *, include_completed=False):
            return (partition,) if include_completed else ()

        def refresh_disclosure_query(self, partition, page):
            return changes

    source = Sources()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0.0, ()), 1)
    context = _CompanyRunContext(NOW, 0.0, _TEST_COMPANY_CONFIGURATION, NOW.date())
    run = _AnnouncementRun(dependencies, state, context)
    monkeypatch.setattr(run, "_report", lambda *args, **kwargs: None)
    assert run._refresh_queries(partition) is (changes is not None)
    result = next(event.result for event in events if event.result is not None)
    if changes is None:
        assert result.outcome == "unchanged" and result.commit_operation_id is None and result.commit_seconds is None
        assert result.changed_rows is None
    else:
        assert result.outcome == "empty" and result.commit_operation_id is not None and result.changed_rows == 0


@pytest.mark.parametrize("branch", ("split", "refresh", "coverage_repair"))
def test_directory_recovery_reports_retry_after_actual_requeue(branch, monkeypatch):
    events = []
    partition = DisclosurePartition("sh", NOW.date(), NOW.date())
    failure = ValueError("cninfo_duplicate_page_links") if branch == "split" else DisclosureCoverageMismatch(1)

    class Index:
        def commit_query_coverage(self, *args):
            raise failure

        def disclosure_members(self, partition):
            return ()

        def split_query(self, partition, repairs):
            return repairs

    class Source(Sources):
        def repair_queries(self, *args, **kwargs):
            return (partition,)

    source = Source()
    dependencies = CompanyEvidenceDependencies(cast(CompanyEvidenceIndex, Index()), source, source, lambda: 0.0, events.append)
    state = _SyncState(CompanyOperations(events.append, lambda: 0.0, ()), 1)
    run = _AnnouncementRun(dependencies, state, _CompanyRunContext(NOW, 0.0, _TEST_COMPANY_CONFIGURATION, NOW.date()))
    monkeypatch.setattr(run, "_report", lambda *args, **kwargs: None)
    if branch == "split":
        def fail():
            raise failure
        with pytest.raises(ValueError):
            state.operations.call("directory", fail, source=source.source, scope=partition)
        monkeypatch.setattr(run, "_split", lambda partition: None)
        assert run._failed(partition, failure) is False
    else:
        monkeypatch.setattr(run, "_refresh_queries", lambda partition: branch == "refresh")
        run._accept(partition, DisclosurePage((), 1, False), verifying=True)
    recovery = next(event.recovery for event in events if event.recovery is not None)
    assert recovery.decision == "retry" and recovery.reason == str(failure)


@pytest.mark.parametrize("stage", ("publication", "finish", "document_commit"))
def test_real_use_case_fatal_database_boundary_reports_then_preserves_failure(tmp_path, monkeypatch, stage):
    events, failures = [], []
    index = _index(tmp_path)
    item = _disclosure(title="关于收到立案告知书的公告")

    class Source(Sources):
        def fetch(self, query, *args, **kwargs):
            if stage == "document_commit" and isinstance(query, DisclosurePartition) and query.end == NOW.date():
                return DisclosurePage((item,), 1, False)
            return super().fetch(query, *args, **kwargs)

        def extract_document(self, *args, **kwargs):
            return _extraction("本公司收到立案告知书。" * 10)

    source = Source()
    original = sqlite3.OperationalError("fatal database failure")

    def fail(*args, **kwargs):
        raise original

    monkeypatch.setattr(index, {"publication": "publish", "finish": "finish", "document_commit": "commit_document"}[stage], fail)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0.0, events.append,
        failures=lambda context, exc: failures.append((context, exc)), run_id="database-failure-run")
    with pytest.raises(sqlite3.OperationalError) as raised:
        _sync_company_evidence(dependencies, ("600001",), NOW)
    assert raised.value is original and len(failures) == 1
    context, failure = failures[0]
    assert failure is original and context.operation_id is not None and context.operation_kind == stage
    assert context.endpoint.startswith("sqlite:")
    terminal = events[-1]
    assert terminal.component == "complete" and terminal.pending is terminal.pending_documents is None
    assert terminal.status == "deferred" and terminal.run_id == "database-failure-run"
    if stage == "document_commit":
        assert context.raw_saved is context.text_extracted is True
        assert not any(event.result is not None and event.result.outcome == "accepted"
                       and event.result.metadata == item for event in events)
