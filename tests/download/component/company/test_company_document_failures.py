"""Document workers retain resource ownership and caller-owned acceptance."""

from concurrent.futures import Future
from dataclasses import replace
from datetime import timedelta
from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.application import company_documents
from trader.download.application.company_documents import CompanyDocumentRun
from trader.download.application.company_evidence import CompanyEvidenceDependencies, _CompanyRunContext, _SyncState
from trader.download.application.company_operations import CompanyOperations
from trader.download.domain.company_evidence import CompanyDocumentFailure, CompanyDocumentTask, DisclosurePage, DisclosurePartition
from trader.infra.http_contracts import HttpTimeout
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.workers import BoundedExecutor, WorkerResourceRejectedError


def _prepare(tmp_path, count=1):
    index = fixtures._index(tmp_path)
    items = tuple(fixtures._disclosure(str(number), "关于收到立案告知书的公告", when=fixtures.NOW - timedelta(seconds=number))
                  for number in range(count))
    index.prepare_and_publish(items[0].codes, fixtures.NOW)
    index.commit_page(DisclosurePartition("sse", fixtures.NOW.date(), fixtures.NOW.date()),
                      DisclosurePage(items, count, False), fixtures.NOW)
    return index, items


def _run(index, source, *, configuration=None, executor=None, cancelled=None, progress=None):
    configuration = configuration or replace(fixtures._TEST_COMPANY_CONFIGURATION, text_workers=1, ocr_workers=1)
    dependencies = CompanyEvidenceDependencies(
        index, source, source, lambda: 0, progress, cancel_requested=cancelled,
        text_executor=executor, ocr_executor=executor,
    )
    operations = CompanyOperations(progress, lambda: 0, (), cancel_requested=cancelled)
    state = _SyncState(operations, 1)
    context = _CompanyRunContext(fixtures.NOW, 0, configuration, fixtures.NOW.date() - timedelta(days=365))
    return CompanyDocumentRun(dependencies, ("600001",), state, context, publish=lambda: None), state


def _body(index, identity="0"):
    with SqliteSession(index.path) as connection:
        return tuple(connection.run("SELECT body_checked,body_retained FROM disclosures WHERE source_key=?", (identity,)).require_one())


@pytest.mark.parametrize("stage", ("download", "text", "ocr", "ocr_handoff"))
def test_cancelled_worker_result_is_not_accepted_or_forwarded(tmp_path, monkeypatch, stage):
    index, items = _prepare(tmp_path)
    cancelled = Event()

    class Source(fixtures.Sources):
        stages = ()

        def document_ready(self, item):
            return stage != "download"

        def download_document(self, item):
            self.stages += ("download",)
            cancelled.set()

        def extract_document(self, item, *, route="standard", page_progress=None):
            self.stages += (route,)
            cancelled.set()
            if stage == "ocr_handoff":
                raise CompanyDocumentFailure("disclosure_document_ocr_required")
            return fixtures._extraction("本公司收到立案告知书。")

    if stage == "ocr":
        monkeypatch.setattr(index, "pending_documents", lambda *args, **kwargs: (CompanyDocumentTask(items[0], "ocr"),))
    source = Source()
    run, state = _run(index, source, cancelled=cancelled.is_set)
    run.run_batch()
    assert len(source.stages) == 1 and state.failures == () and run.completed == 0
    assert _body(index) == (0, 0)
    assert not run._pending and not state.operations._submitted
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM document_retries").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM document_tasks WHERE stage='ocr_pending'").require_one()[0] == 0


@pytest.mark.parametrize("stage", ("download", "text", "ocr"))
def test_queued_cancellation_prevents_the_actual_worker_callback(tmp_path, monkeypatch, stage):
    index, items = _prepare(tmp_path)
    cancelled = Event()

    class Source(fixtures.Sources):
        worker_calls = 0

        def document_ready(self, item):
            return stage != "download"

        def download_document(self, item):
            self.worker_calls += 1

        def extract_document(self, item, *, route="standard", page_progress=None):
            self.worker_calls += 1
            return fixtures._extraction("本公司收到立案告知书。")

    def observe(event):
        if event.operation is not None and event.operation.kind == stage and event.operation.state == "queued":
            cancelled.set()

    if stage == "ocr":
        monkeypatch.setattr(index, "pending_documents", lambda *args, **kwargs: (CompanyDocumentTask(items[0], "ocr"),))
    source = Source()
    run, state = _run(index, source, cancelled=cancelled.is_set, progress=observe)
    run.run_batch()
    assert source.worker_calls == 0 and _body(index) == (0, 0) and state.failures == ()
    assert not run._pending and not state.operations._submitted


def test_cancellation_inside_an_already_accepted_commit_finishes_that_transaction(tmp_path, monkeypatch):
    index, _items = _prepare(tmp_path)
    cancelled = Event()
    commit = index.commit_document

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def extract_document(self, item, *, route="standard", page_progress=None):
            return fixtures._extraction("本公司收到立案告知书。")

    def accepted(item, extraction, observed_at):
        cancelled.set()
        return commit(item, extraction, observed_at)

    monkeypatch.setattr(index, "commit_document", accepted)
    run, state = _run(index, Source(), cancelled=cancelled.is_set)
    run.run_batch()
    assert _body(index) == (1, 1) and run.completed == 1 and state.changes == 1


def test_queued_commit_cancellation_does_not_begin_the_transaction(tmp_path):
    index, _items = _prepare(tmp_path)
    cancelled = Event()
    events = []

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def extract_document(self, item, *, route="standard", page_progress=None):
            return fixtures._extraction("本公司收到立案告知书。")

    def observe(event):
        events.append(event)
        if event.operation is not None and event.operation.kind == "document_commit" and event.operation.state == "queued":
            cancelled.set()

    run, state = _run(index, Source(), cancelled=cancelled.is_set, progress=observe)
    run.run_batch()
    assert _body(index) == (0, 0) and run.completed == state.changes == 0
    assert not any(event.result is not None and event.result.outcome in {"accepted", "rejected"} for event in events)
    assert state.failures == () and not state.operations._submitted


@pytest.mark.parametrize("stage", ("download", "text", "ocr"))
@pytest.mark.parametrize("phase", ("queued", "running", "completed"))
def test_worker_observer_error_is_not_a_document_failure(tmp_path, monkeypatch, stage, phase):
    index, items = _prepare(tmp_path)

    class Source(fixtures.Sources):
        worker_calls = 0

        def document_ready(self, item):
            return stage != "download"

        def download_document(self, item):
            self.worker_calls += 1

        def extract_document(self, item, *, route="standard", page_progress=None):
            self.worker_calls += 1
            return fixtures._extraction("本公司收到立案告知书。")

    def observe(event):
        if event.operation is not None and event.operation.kind == stage and event.operation.state == phase:
            raise ValueError("fixture_worker_observer_failed")

    if stage == "ocr":
        monkeypatch.setattr(index, "pending_documents", lambda *args, **kwargs: (CompanyDocumentTask(items[0], "ocr"),))
    source = Source()
    run, state = _run(index, source, progress=observe)
    with pytest.raises(ValueError, match="fixture_worker_observer_failed"):
        run.run_batch()
    assert source.worker_calls == int(phase == "completed")
    assert state.failures == () and state.failed_documents == 0 and _body(index) == (0, 0)
    assert not run._pending and not state.operations._submitted
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM document_retries").require_one()[0] == 0


def test_ocr_page_observer_failure_remains_local(tmp_path, monkeypatch):
    index, items = _prepare(tmp_path)
    monkeypatch.setattr(index, "pending_documents", lambda *args, **kwargs: (CompanyDocumentTask(items[0], "ocr"),))

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def extract_document(self, item, *, route="standard", page_progress=None):
            page_progress(1, 2)
            return fixtures._extraction("本公司收到立案告知书。")

    def observe(event):
        if event.operation is not None and event.operation.kind == "ocr" and event.operation.completed_units == 1:
            raise RuntimeError("fixture_ocr_observer_failed")

    run, state = _run(index, Source(), progress=observe)
    with pytest.raises(RuntimeError, match="fixture_ocr_observer_failed"):
        run.run_batch()
    assert state.failures == () and _body(index) == (0, 0)
    assert not run._pending and not state.operations._submitted


@pytest.mark.parametrize("failure", ("commit", "take_future", "wait", "discover", "failure_receipt"))
def test_fatal_batch_error_drains_all_other_owned_work_without_accepting_it(tmp_path, monkeypatch, failure):
    index, _items = _prepare(tmp_path, 2)
    entered, release, finished = Event(), Event(), Event()

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def extract_document(self, item, *, route="standard", page_progress=None):
            if item.identity == "1":
                entered.set()
                assert release.wait(5)
                finished.set()
            else:
                assert entered.wait(5)
                if failure == "failure_receipt":
                    raise HttpTimeout("fixture_worker_timeout")
            return fixtures._extraction("本公司收到立案告知书。")

    def fail(*args, **kwargs):
        assert entered.wait(5)
        release.set()
        raise RuntimeError("fixture_batch_failed")

    executor = BoundedExecutor(worker_count=2, queue_capacity=0, thread_name_prefix="company-document-failure")
    executor.start()
    configuration = replace(fixtures._TEST_COMPANY_CONFIGURATION, text_workers=2, document_batch_size=2)
    run, state = _run(index, Source(), executor=executor, configuration=configuration)
    if failure == "commit":
        monkeypatch.setattr(index, "commit_document", fail)
    elif failure == "take_future":
        monkeypatch.setattr(state.operations, "take_future", fail)
    elif failure == "wait":
        monkeypatch.setattr(company_documents, "wait", fail)
    elif failure == "failure_receipt":
        monkeypatch.setattr(index, "document_failure", fail)
    try:
        with pytest.raises((RuntimeError, HttpTimeout)):
            run.run_batch(discover=fail if failure == "discover" else None)
        assert finished.is_set() and not run._pending and not state.operations._submitted
        assert run.completed == 0 and _body(index, "1") == (0, 0)
    finally:
        release.set()
        executor.stop(wait=True, cancel_futures=True)


def test_unexpected_cancelled_future_is_a_resource_failure(tmp_path):
    index, _items = _prepare(tmp_path)

    class Executor:
        def submit(self, function):
            future = Future()
            future.cancel()
            future.set_running_or_notify_cancel()
            return future

    run, state = _run(index, fixtures.Sources())
    run.executor = Executor()
    with pytest.raises(WorkerResourceRejectedError, match="company_document_request_cancelled"):
        run.run_batch()
    assert state.failures == () and not state.operations._submitted


def test_stopped_download_source_still_processes_later_local_inputs(tmp_path):
    index, items = _prepare(tmp_path, 2)
    index.document_stage(items[1], "downloaded")
    with SqliteSession(index.path) as connection:
        connection.run("UPDATE document_tasks SET path='raw/local.pdf'")
    events = []

    class Source(fixtures.Sources):
        worker_calls = ()

        def document_ready(self, item):
            return item.identity == "1"

        def download_document(self, item):
            raise AssertionError("stopped source must not download")

        def extract_document(self, item, *, route="standard", page_progress=None):
            self.worker_calls += (item.identity,)
            return fixtures._extraction("本公司收到立案告知书。")

    source = Source()
    run, _state = _run(index, source, progress=events.append)
    run.stopped = True
    assert run.has_due()
    run.run_batch()
    assert source.worker_calls == ("1",) and _body(index, "1") == (1, 1) and _body(index, "0") == (0, 0)
    assert not run.has_due()
    assert events[-1].status == "deferred"


def test_unfinished_document_inventory_is_reported_as_deferred(tmp_path):
    index, _items = _prepare(tmp_path)
    events = []

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def extract_document(self, item, *, route="standard", page_progress=None):
            return None

    run, _state = _run(index, Source(), progress=events.append)
    run.run_batch()
    assert events[-1].status == "deferred" and events[-1].pending == 1


@pytest.mark.parametrize("all_stale", (False, True))
def test_stale_local_paths_do_not_hide_ready_inputs_or_repeat_forever(tmp_path, all_stale):
    index, items = _prepare(tmp_path, 3)
    for item in items:
        index.document_stage(item, "downloaded")
    with SqliteSession(index.path) as connection:
        connection.run("UPDATE document_tasks SET path='raw/missing.pdf'")

    class Source(fixtures.Sources):
        checked = ()
        downloads = ()

        def document_ready(self, item):
            self.checked += (item.identity,)
            return not all_stale and item.identity == "2"

        def download_document(self, item):
            self.downloads += (item.identity,)

        def extract_document(self, item, *, route="standard", page_progress=None):
            return fixtures._extraction("本公司收到立案告知书。")

    source = Source()
    configuration = replace(fixtures._TEST_COMPANY_CONFIGURATION, document_batch_size=1)
    run, state = _run(index, source, configuration=configuration)
    run.stopped = True
    batches = 0
    while run.has_due():
        batches += 1
        assert batches <= 3
        run.run_batch()
    assert batches == 3 and source.checked == ("0", "1", "2") and not source.downloads
    assert _body(index, "2") == ((0, 0) if all_stale else (1, 1))
    assert state.failures == () and state.failed_documents == 0
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM document_retries").require_one()[0] == 0
    # A fresh normal continuation can download the absent material again.
    resumed, _resumed_state = _run(index, source, configuration=configuration)
    resumed.run_batch()
    assert source.downloads == ("0",) and _body(index, "0") == (1, 1)


def test_worker_failure_is_preserved_when_the_failed_observer_also_raises(tmp_path):
    index, _items = _prepare(tmp_path)
    primary = CompanyDocumentFailure("disclosure_document_no_text")

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return True

        def extract_document(self, item, *, route="standard", page_progress=None):
            raise primary

    def observe(event):
        if event.operation is not None and event.operation.kind == "text" and event.operation.state == "failed":
            raise ValueError("fixture_secondary_observer_failure")

    run, state = _run(index, Source(), progress=observe)
    run.run_batch()
    assert state.failures == ("risk_documents:disclosure_document_no_text",) and state.failed_documents == 1
    assert not run._pending and not state.operations._submitted and _body(index) == (0, 0)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT failure FROM document_retries").require_one()[0] == primary.reason
