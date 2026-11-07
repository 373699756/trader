"""Explicit scheduling candidates preserve sole-writer and recovery boundaries."""

from concurrent.futures import Future
from dataclasses import replace
from threading import Event, get_ident
from unittest.mock import patch

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.application.company_evidence import (
    CompanyEvidenceDependencies,
    SyncCompanyEvidence,
    _AnnouncementRun,
    _CompanyRunContext,
    _DocumentRun,
    _SyncState,
)
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.application.company_operations import CompanyOperations
from trader.infra.workers import BoundedExecutor, WorkerResourceRejectedError


def _announcements(index, source, executor, *, cancelled=None, clock=None, coalesce=False):
    configuration = fixtures._TEST_COMPANY_CONFIGURATION
    dependencies = CompanyEvidenceDependencies(
        index,
        source,
        source,
        (lambda: 0.0) if clock is None else (lambda: clock[0]),
        cancel_requested=cancelled,
        directory_executor=executor,
        coalesce_publications=coalesce,
    )
    context = _CompanyRunContext(fixtures.NOW, 0.0, configuration, fixtures.NOW.date())
    state = _SyncState(CompanyOperations(None, dependencies.monotonic, ()), 1)
    run = _AnnouncementRun(dependencies, state, context)
    run.prepare(("600001",))
    return run, state


@pytest.mark.parametrize("cancel", (False, True))
def test_prefetch_is_bounded_and_only_main_thread_accepts_pages(tmp_path, monkeypatch, cancel):
    index = fixtures._index(tmp_path)
    main_thread = get_ident()
    entered, release, cancelled = Event(), Event(), Event()
    requests, commits = [], []
    original_commit = index.commit_page

    def commit(partition, page, observed_at):
        assert get_ident() == main_thread
        commits.append(partition)
        return original_commit(partition, page, observed_at)

    class Source(fixtures.Sources):
        def fetch(self, partition):
            assert get_ident() != main_thread
            requests.append(partition)
            entered.set()
            assert release.wait(5)
            return DisclosurePage((), 0, False)

    monkeypatch.setattr(index, "commit_page", commit)
    executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-candidate-test")
    executor.start()
    run, state = _announcements(index, Source(), executor, cancelled=cancelled.is_set)
    try:
        assert run.run_batch(1, wait_for_page=False) > 0
        assert entered.wait(5)
        assert not commits and state.pages == 0
        # Polling while the page is in flight must neither queue a second
        # request nor advance the durable directory cursor.
        run.run_batch(1, wait_for_page=False)
        assert len(requests) == 1 and not commits
        if cancel:
            cancelled.set()
        release.set()
        if not cancel:
            run.run_batch(1)
            assert len(commits) == state.pages == 1
        else:
            run.run_batch(1, wait_for_page=False)
            assert not commits and state.pages == 0
    finally:
        release.set()
        run.close()
        executor.stop(wait=True, cancel_futures=True)
    assert requests[0].end == fixtures.NOW.date()


def test_prefetch_failure_is_recorded_on_main_thread(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    main_thread = get_ident()
    failures = []
    original_failure = index.partition_failure

    def failed(partition, failure):
        assert get_ident() == main_thread
        failures.append(failure)
        original_failure(partition, failure)

    class Source(fixtures.Sources):
        def fetch(self, partition):
            raise TimeoutError("fixture")

    monkeypatch.setattr(index, "partition_failure", failed)
    executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-candidate-test")
    executor.start()
    run, state = _announcements(index, Source(), executor)
    try:
        run.run_batch(1)
    finally:
        run.close()
        executor.stop(wait=True, cancel_futures=True)
    assert failures and state.pages == 0
    assert any("announcements:" in failure for failure in state.failures)


def test_publish_deadline_survives_directory_wait(tmp_path):
    index = fixtures._index(tmp_path)
    clock = [29.0]
    run, state = _announcements(index, fixtures.Sources(), None, clock=clock, coalesce=True)
    run._context = replace(run._context, configuration=replace(run._context.configuration, publication_interval_seconds=30))
    state.publication_pending_since = 0.0
    state.unpublished_documents = 1
    with patch("trader.download.application.company_evidence._publish_batch") as publish:
        run._publish_due()
        publish.assert_not_called()
        clock[0] = 30.0
        run._publish_due()
        publish.assert_called_once()


def test_document_batch_does_not_reset_oldest_unpublished_time(tmp_path):
    index = fixtures._index(tmp_path)
    source = fixtures.Sources()
    state = _SyncState(CompanyOperations(None, lambda: 20.0, ()), 1,
                       unpublished_documents=1, publication_pending_since=10.0)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 20.0, coalesce_publications=True)
    context = _CompanyRunContext(fixtures.NOW, 0.0, fixtures._TEST_COMPANY_CONFIGURATION, fixtures.NOW.date())
    documents = _DocumentRun(dependencies, ("600001",), state, context)
    documents.run_batch()
    documents.run_batch()
    assert state.unpublished_documents == 1 and state.publication_pending_since == 10.0


def test_cancelled_prefetch_is_not_accepted(tmp_path):
    index = fixtures._index(tmp_path)

    class Executor:
        def __init__(self):
            self.future = Future()

        def submit(self, function, /, *args, **kwargs):
            return self.future

    executor = Executor()
    run, state = _announcements(index, fixtures.Sources(), executor)
    run.run_batch(1, wait_for_page=False)
    run.close()
    assert executor.future.cancelled() and state.pages == 0


def test_executor_cancellation_keeps_directory_pending(tmp_path):
    index = fixtures._index(tmp_path)

    class Executor:
        def submit(self, function, /, *args, **kwargs):
            future = Future()
            future.cancel()
            return future

    run, state = _announcements(index, fixtures.Sources(), Executor())
    with pytest.raises(WorkerResourceRejectedError, match="company_directory_request_cancelled"):
        run.run_batch(1, wait_for_page=False)
    assert run.can_continue and state.pages == 0
    assert run.run_batch(0) > 0


def test_publication_failure_does_not_discard_inflight_directory_request(tmp_path):
    index = fixtures._index(tmp_path)
    clock = [0.0]

    class Executor:
        def __init__(self):
            self.future = Future()

        def submit(self, function, /, *args, **kwargs):
            return self.future

    executor = Executor()
    run, state = _announcements(index, fixtures.Sources(), executor, clock=clock, coalesce=True)
    run.run_batch(1, wait_for_page=False)
    state.publication_pending_since = 0.0
    clock[0] = 60.0
    with patch("trader.download.application.company_evidence._publish_batch", side_effect=RuntimeError("publish_failed")):
        with pytest.raises(RuntimeError, match="publish_failed"):
            run.run_batch(1, wait_for_page=False)
    assert run._prefetch is not None and not state.failures
    run.close()
    assert executor.future.cancelled() and state.pages == 0


def test_coalesced_cancel_keeps_accepted_body_without_querying_urgent_state(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    cancelled = Event()

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                present = query.market == "sse" and query.start <= item.published_at.date() <= query.end
                return DisclosurePage((item,) if present else (), int(present), False)
            return super().fetch(query, **kwargs)

        def document(self, item):
            cancelled.set()
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    def urgent(item):
        pytest.fail("cancelled sync must not open the field-repair inventory")

    monkeypatch.setattr(index, "document_publication_urgent", urgent)
    item = fixtures._disclosure("cancelled-body", "关于收到立案告知书的公告")
    partition = DisclosurePartition("sse", fixtures.NOW.date(), fixtures.NOW.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    source = Source()
    dependencies = CompanyEvidenceDependencies(
        index, source, source, lambda: 0.0,
        cancel_requested=cancelled.is_set, coalesce_publications=True,
    )
    request = fixtures.CompanyEvidenceRequest(("600001",), fixtures.NOW, fixtures._TEST_COMPANY_CONFIGURATION)
    result = SyncCompanyEvidence(dependencies, request)
    assert result.needs_retry and result.pending_documents is None
    cancelled.clear()
    resumed = SyncCompanyEvidence(dependencies, request)
    assert resumed.published_codes == 1 and resumed.pending_documents == 0
