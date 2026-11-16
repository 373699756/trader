"""News and directory recovery preserve finite failure and cancellation outcomes."""

import sqlite3
from dataclasses import replace
from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract, _read, _Sources
from trader.download.application.company_evidence import CompanyEvidenceDependencies, _SyncState, _news
from trader.download.application.company_operations import CompanyOperations
from trader.download.domain.company_evidence import DisclosurePartition, DisclosureQuery
from trader.download.infra.company_facts_index import ReadCompanyPublicationIdentity
from trader.infra.sqlite_connection import SqliteSession
from trader.infra.workers import BoundedExecutor


def _identity(index):
    with SqliteSession(index.facts_path) as connection:
        return ReadCompanyPublicationIdentity(connection)


def _news_item():
    return replace(fixtures._disclosure("late-news", "公司经营动态"), source="eastmoney_news", kind="news")


@pytest.mark.parametrize("coalesce", (False, True))
def test_news_sqlite_failure_stops_sync_without_a_successful_finish_or_new_publication(tmp_path, coalesce):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    events = []

    class Source(_Sources):
        before_news = None
        news_calls = 0

        def news(self, code, *, observed_at):
            self.before_news = _identity(index)
            self.news_calls += 1
            return (_news_item(),)

    source = Source(contract)
    with SqliteSession(index.path) as connection:
        connection.run(
            "CREATE TRIGGER fail_news BEFORE INSERT ON disclosures WHEN NEW.kind='news' "
            "BEGIN SELECT RAISE(ABORT,'fixture_disk_failure'); END"
        )
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append, coalesce_publications=coalesce)
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable") as error:
        fixtures._sync_company_evidence(dependencies, contract.codes, fixtures.NOW, news_codes=contract.codes)
    assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    assert source.news_calls == 1 and source.before_news is not None and _identity(index) == source.before_news
    assert _read(index).positive_events
    assert not any(event.component == "finish" for event in events)
    assert not any(event.recovery is not None and event.recovery.component == "news" for event in events)
    assert events[-1].component == "complete" and events[-1].status == "deferred"
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM disclosures WHERE kind='news'").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM component_checks WHERE component='news'").require_one()[0] == 0
        connection.run("DROP TRIGGER fail_news")
    resumed_source = Source(contract)
    resumed = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, resumed_source, resumed_source, lambda: 0, coalesce_publications=coalesce),
        contract.codes, fixtures.NOW, news_codes=contract.codes,
    )
    assert resumed.published_codes == 1 and resumed_source.news_calls == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM disclosures WHERE kind='news'").require_one()[0] == 1


@pytest.mark.parametrize("coalesce", (False, True))
@pytest.mark.parametrize("late_failure", (False, True))
def test_cancelled_news_response_is_not_committed_and_keeps_cancelled_counts_unknown(tmp_path, coalesce, late_failure):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    cancelled = Event()
    events = []

    class Source(_Sources):
        before_news = None

        def news(self, code, *, observed_at):
            self.before_news = _identity(index)
            cancelled.set()
            if late_failure:
                raise OSError("fixture_cancelled_source")
            return (_news_item(),)

    source = Source(contract)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(
            index, source, source, lambda: 0, events.append,
            cancel_requested=cancelled.is_set, coalesce_publications=coalesce,
        ),
        contract.codes, fixtures.NOW, news_codes=contract.codes,
    )
    assert result.failures == ("task:cancelled",) and result.needs_retry
    assert result.pending_partitions is result.pending_documents is None
    assert source.before_news is not None and _identity(index) == source.before_news
    assert not any(event.component == "finish" for event in events)
    assert not any(event.result is not None and event.result.component == "news" for event in events)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM disclosures WHERE kind='news'").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM component_checks WHERE component='news'").require_one()[0] == 0


@pytest.mark.parametrize("phase", ("queued", "completed", "result"))
def test_news_observation_failure_propagates_without_source_cooldown(tmp_path, phase):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    before = _identity(index)

    class Source(_Sources):
        news_calls = 0

        def news(self, code, *, observed_at):
            self.news_calls += 1
            return (_news_item(),)

    def observe(event):
        if phase == "result" and event.result is not None and event.result.component == "news":
            raise RuntimeError("fixture_news_observation_failed")
        if (event.operation is not None and event.operation.kind == "news"
            and event.operation.source == source.news_source and event.operation.state == phase):
            raise RuntimeError("fixture_news_observation_failed")

    source = Source(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _SyncState(CompanyOperations(observe, lambda: 0, ()), 1)
    with pytest.raises(RuntimeError, match="fixture_news_observation_failed"):
        _news(dependencies, contract.codes, fixtures.NOW, state)
    assert source.news_calls == int(phase != "queued")
    assert state.failures == () and _identity(index) == before
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM disclosures WHERE kind='news'").require_one()[0] == int(phase == "result")
        checks = tuple(connection.run("SELECT failure FROM component_checks WHERE component='news'"))
        assert all(row[0] == "" for row in checks)
    if phase == "result":
        resumed = _SyncState(CompanyOperations(None, lambda: 0, ()), 1)
        assert _news(dependencies, contract.codes, fixtures.NOW, resumed) == ()
        assert source.news_calls == 1 and resumed.reused == 1


@pytest.mark.parametrize("error", (TimeoutError("fixture"), RuntimeError("fixture"), ValueError("fixture")))
def test_news_supplier_failure_retains_the_existing_finite_degraded_result(tmp_path, error):
    index = fixtures._index(tmp_path)
    contract = _contract(index)

    class Source(_Sources):
        news_calls = ()

        def news(self, code, *, observed_at):
            self.news_calls += (code,)
            raise error

    source = Source(contract)
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _SyncState(CompanyOperations(None, lambda: 0, ()), 2)
    assert _news(dependencies, ("600001", "600002"), fixtures.NOW, state) == ("600001",)
    assert source.news_calls == ("600001",) and len(state.failures) == 1
    assert state.failures[0].startswith("news:")


@pytest.mark.parametrize("split", ("dates", "queries"))
@pytest.mark.parametrize("prefetch", (False, True))
def test_sqlite_failure_during_directory_recovery_stops_before_more_requests_or_cooldown(tmp_path, monkeypatch, split, prefetch):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    partitions = index.partitions(("sse",), fixtures.NOW)
    target = (
        max((item for item in partitions if item.start < item.end), key=lambda item: item.end)
        if split == "dates" else max(partitions, key=lambda item: item.end)
    )
    remaining = min(partitions, key=lambda item: item.end)
    monkeypatch.setattr(index, "partitions", lambda *args: (target, remaining))

    class Source(_Sources):
        directory_calls = ()
        before_failure = None

        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                self.directory_calls += (query,)
                self.before_failure = _identity(index)
                raise ValueError("cninfo_partition_changed" if split == "dates" else "cninfo_page_limit")
            return super().fetch(query, **kwargs)

        def split_queries(self, partition):
            return (DisclosureQuery("shmb"), DisclosureQuery("shkcp"))

    source = Source(contract)
    with SqliteSession(index.path) as connection:
        connection.run(
            "CREATE TRIGGER fail_split BEFORE DELETE ON partitions BEGIN SELECT RAISE(ABORT,'fixture_split_failure'); END"
            if split == "dates" else
            "CREATE TRIGGER fail_split BEFORE INSERT ON disclosure_queries BEGIN SELECT RAISE(ABORT,'fixture_split_failure'); END"
        )
    executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="company-recovery-test") if prefetch else None
    if executor is not None:
        executor.start()
    try:
        with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable") as error:
            fixtures._sync_company_evidence(
                CompanyEvidenceDependencies(index, source, source, lambda: 0, directory_executor=executor),
                contract.codes, fixtures.NOW,
            )
        assert isinstance(error.value.__cause__, sqlite3.IntegrityError)
    finally:
        if executor is not None:
            executor.stop(wait=True, cancel_futures=True)
    assert source.directory_calls == (target,)
    assert source.before_failure is not None and _identity(index) == source.before_failure
    assert _read(index).positive_events
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM partition_retries").require_one()[0] == 0
        assert connection.run("SELECT count(*) FROM partitions WHERE failure<>''").require_one()[0] == 0
