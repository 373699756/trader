"""Directory recovery distinguishes supplier responses from local state and observers."""

from collections import deque
from datetime import timedelta
from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract, _Sources
from trader.download.application import company_evidence
from trader.download.application.company_evidence import CompanyEvidenceDependencies, _AnnouncementRun, _CompanyRunContext, _SyncState
from trader.download.application.company_operations import CompanyOperations
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition, DisclosureQuery
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.workers import BoundedExecutor


def _identity(index):
    with SqliteSession(index.facts_path) as connection:
        return ReadCompanyPublicationIdentity(connection)


def _run(index, source, target, *, executor=None, cancelled=None, progress=None):
    dependencies = CompanyEvidenceDependencies(
        index, source, source, lambda: 0, progress,
        directory_executor=executor, cancel_requested=cancelled,
    )
    state = _SyncState(CompanyOperations(progress, lambda: 0, ()), 1)
    context = _CompanyRunContext(fixtures.NOW, 0, fixtures._TEST_COMPANY_CONFIGURATION, fixtures.NOW.date() - timedelta(days=365))
    run = _AnnouncementRun(dependencies, state, context)
    run.prepare(("600001",))
    run._queue = deque((target,))
    return run, state


@pytest.fixture(params=(False, True))
def directory_executor(request):
    executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-directory-test") if request.param else None
    if executor is not None:
        executor.start()
    try:
        yield executor
    finally:
        if executor is not None:
            executor.stop(wait=True, cancel_futures=True)


@pytest.mark.parametrize("local_failure", ("codec", "contract"))
def test_local_query_failure_keeps_saved_child_cursor_and_does_not_create_cooldown(tmp_path, local_failure):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    target = index.partitions(("sse",), fixtures.NOW)[0]
    child, = index.split_query(target, (DisclosureQuery("shmb"),))
    first, second = (fixtures._disclosure(key) for key in ("saved-child", "uncommitted-child"))
    index.commit_page(child, DisclosurePage((first,), 2, True), fixtures.NOW)
    with SqliteSession(index.path) as connection:
        if local_failure == "codec":
            connection.run("UPDATE disclosure_queries SET securities='{}'")
        else:
            connection.run("PRAGMA ignore_check_constraints=ON")
            connection.run("UPDATE disclosure_query_contracts SET filter_identity='invalid'")

    class Source(_Sources):
        directory_calls = ()
        before_fetch = None

        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                self.directory_calls += (query,)
                self.before_fetch = _identity(index)
                return DisclosurePage((second,), 2, False)
            return super().fetch(query, **kwargs)

    source = Source(contract)
    expected = "cninfo_query_invalid" if local_failure == "codec" else "company_query_contract_mismatch"
    with pytest.raises(ValueError, match=expected):
        fixtures._sync_company_evidence(
            CompanyEvidenceDependencies(index, source, source, lambda: 0), contract.codes, fixtures.NOW,
        )
    assert len(source.directory_calls) == int(local_failure == "contract")
    if source.before_fetch is not None:
        assert _identity(index) == source.before_fetch
    with SqliteSession(index.path) as connection:
        row = connection.run("SELECT next_page,received_rows,is_complete FROM disclosure_queries").require_one()
        assert tuple(row) == (2, 1, 0)
        assert connection.run("SELECT count(*) FROM disclosure_query_rows").require_one()[0] == 1
        assert connection.run("SELECT count(*) FROM disclosures WHERE source_key='uncommitted-child'").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM partition_retries").require_one()[0] == 0


@pytest.mark.parametrize("phase", ("queued", "completed", "result"))
def test_directory_observer_failure_propagates_for_sync_and_prefetch(tmp_path, directory_executor, phase):
    index = fixtures._index(tmp_path)
    _contract(index)
    target = index.partitions(("sse",), fixtures.NOW)[0]
    item = fixtures._disclosure("observed-directory")

    class Source(fixtures.Sources):
        requests = 0

        def fetch(self, partition):
            self.requests += 1
            return DisclosurePage((item,), 1, False)

    source = Source()

    def observe(event):
        if phase == "result" and event.result is not None and event.result.component == "announcements":
            raise RuntimeError("fixture_directory_observer_failed")
        if (event.operation is not None and event.operation.kind == "directory"
            and event.operation.source == source.source and event.operation.state == phase):
            raise RuntimeError("fixture_directory_observer_failed")

    run, state = _run(index, source, target, executor=directory_executor, progress=observe)
    before = _identity(index)
    try:
        with pytest.raises(RuntimeError, match="fixture_directory_observer_failed"):
            run.run_batch(1)
    finally:
        run.close()
    assert source.requests == int(phase != "queued") and state.failures == ()
    assert _identity(index) == before
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM partition_retries").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0] == int(phase == "result")


@pytest.mark.parametrize("error", (RuntimeError("fixture_source_failed"), ValueError("fixture_source_invalid")))
def test_actual_directory_supplier_failure_remains_deferred_for_sync_and_prefetch(tmp_path, directory_executor, error):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), fixtures.NOW)[0]

    class Source(fixtures.Sources):
        requests = 0

        def fetch(self, partition):
            self.requests += 1
            raise error

    source = Source()
    run, state = _run(index, source, target, executor=directory_executor)
    try:
        assert run.run_batch(1) == 1
    finally:
        run.close()
    assert source.requests == 1 and len(state.failures) == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM partition_retries").require_one()[0] == 1


@pytest.mark.parametrize("failure", ("refresh_commit", "refresh_cancel", "repair_observer", "repair_members"))
def test_query_reconciliation_does_not_convert_local_failures_or_cancelled_results_to_source_work(tmp_path, monkeypatch, failure):
    index = fixtures._index(tmp_path)
    _contract(index)
    target = index.partitions(("sse",), fixtures.NOW)[0]
    child, = index.split_query(target, (DisclosureQuery("shmb"),))
    first, second = (fixtures._disclosure(key) for key in ("known-query-row", "new-query-row"))
    index.commit_page(child, DisclosurePage((first,), 1, False), fixtures.NOW)
    cancelled = Event()

    class Source(fixtures.Sources):
        requests = ()
        repairs = 0
        in_repair = False

        def fetch(self, partition):
            self.requests += (partition,)
            if partition.query is None:
                return DisclosurePage((first, second), 2, False)
            if failure == "refresh_cancel":
                cancelled.set()
            return DisclosurePage((first, second), 2, False) if failure.startswith("refresh") else DisclosurePage((first,), 1, False)

        def repair_queries(self, partition, members, *, missing_rows, report):
            self.repairs += 1
            self.in_repair = True
            report(partition)
            return (DisclosureQuery("shkcp"),)

    source = Source()

    def observe(event):
        if failure == "repair_observer" and source.in_repair and event.activity == "repair":
            raise RuntimeError("fixture_repair_observer_failed")

    if failure == "refresh_commit":
        with SqliteSession(index.path) as connection:
            connection.run(
                "CREATE TRIGGER fail_refresh BEFORE INSERT ON disclosures WHEN NEW.source_key='new-query-row' "
                "BEGIN SELECT RAISE(ABORT,'fixture_refresh_failure'); END"
            )
    if failure == "repair_members":
        def invalid_members(partition):
            raise ValueError("fixture_local_members_invalid")
        monkeypatch.setattr(index, "disclosure_members", invalid_members)
    run, state = _run(index, source, target, cancelled=cancelled.is_set, progress=observe)
    before = _identity(index)
    try:
        if failure == "refresh_cancel":
            assert run.run_batch(1) == 1
        else:
            with pytest.raises((RuntimeError, ValueError)):
                run.run_batch(1)
    finally:
        run.close()
    assert len(source.requests) == 2 and source.repairs == int(failure == "repair_observer")
    assert state.failures == () and _identity(index) == before
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM partition_retries").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM disclosures WHERE source_key='new-query-row'").require_one()[0] == 0
        assert tuple(connection.run("SELECT is_complete,expected_rows FROM disclosure_queries").require_one()) == (1, 1)


@pytest.mark.parametrize("failure", ("source", "invalid_group"))
def test_split_source_and_output_validation_failures_keep_the_existing_degraded_result(tmp_path, failure):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), fixtures.NOW)[0]

    class Source(fixtures.Sources):
        splits = 0

        def fetch(self, partition):
            raise ValueError("cninfo_page_limit")

        def split_queries(self, partition):
            self.splits += 1
            if failure == "source":
                raise OSError("fixture_split_source_unavailable")
            return (DisclosureQuery("shmb"), DisclosureQuery("shmb"))

    source = Source()
    run, state = _run(index, source, target)
    try:
        assert run.run_batch(1) == 1
    finally:
        run.close()
    assert source.splits == 1 and len(state.failures) == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM partition_retries").require_one()[0] == 1


@pytest.mark.parametrize("failure", ("wait", "take_future"))
def test_local_wait_or_observation_failure_keeps_the_prefetch_owned_until_close(tmp_path, monkeypatch, failure):
    index = fixtures._index(tmp_path)
    target = index.partitions(("sse",), fixtures.NOW)[0]
    entered, release = Event(), Event()

    class Source(fixtures.Sources):
        def fetch(self, partition):
            entered.set()
            assert release.wait(5)
            return DisclosurePage((), 0, False)

    executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-close-test")
    executor.start()
    run, state = _run(index, Source(), target, executor=executor)
    real_wait = company_evidence.wait
    interrupted = False

    def broken_wait(*args, **kwargs):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            assert entered.wait(5)
            raise ValueError("fixture_local_observation_failed")
        return real_wait(*args, **kwargs)

    def broken_observation(future):
        raise ValueError("fixture_local_observation_failed")

    if failure == "wait":
        monkeypatch.setattr(company_evidence, "wait", broken_wait)
    else:
        release.set()
        monkeypatch.setattr(state.operations, "take_future", broken_observation)
    try:
        with pytest.raises(ValueError, match="fixture_local_observation_failed"):
            run.run_batch(1)
        assert run._prefetch is not None and run._prefetch[2].done() == (failure == "take_future")
        release.set()
        run.close()
        assert run._prefetch is None and state.pages == 0 and state.failures == ()
        assert not state.operations._submitted
    finally:
        release.set()
        run.close()
        executor.stop(wait=True, cancel_futures=True)
