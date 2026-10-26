"""Company restart, durable failure and published coverage regression scenarios."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component import test_company_evidence as fixtures
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex, SQLiteCompanyEvidenceReader
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

NOW = fixtures.NOW


def test_document_inventory_switches_progress_before_reading_after_announcement_failure(tmp_path):
    events = []
    inventories = []

    class Index(SQLiteCompanyEvidenceIndex):
        def pending_documents(self, codes, observed_at):
            if not inventories:
                assert events[-2].component == "announcements" and events[-2].status == "deferred"
                assert events[-1].component == "risk_documents" and events[-1].status == "running"
                assert events[-1].pending is None and events[-1].completed == 0
            inventories.append(True)
            return super().pending_documents(codes, observed_at)

    class Source(fixtures.Sources):
        def fetch(self, query, *args, **kwargs):
            if isinstance(query, DisclosurePartition):
                raise TimeoutError("fixture")
            return super().fetch(query, *args, **kwargs)

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(Index(tmp_path / "company.sqlite3"), source, source, lambda: 0, events.append),
        ("600001",),
        NOW,
    )
    assert inventories and result.needs_retry and result.published_codes == 1


def test_recent_day_planning_never_duplicates_a_completed_wider_owner(tmp_path):
    index = fixtures._index(tmp_path)
    index.partitions(("sse",), NOW)
    start, end = NOW.date() - timedelta(days=8), NOW.date() - timedelta(days=1)
    with SqliteSession(index.path) as c, SqliteTransaction(c):
        c.run("DELETE FROM partitions WHERE start>=? AND end<=?", (str(start), str(end)))
        c.run(
            "INSERT INTO partitions(market,start,end,complete,checked_at) VALUES('sse',?,?,1,?)",
            (str(start), str(end), NOW.isoformat()),
        )
    index.partitions(("sse",), NOW)
    with SqliteSession(index.path) as c:
        assert (
            c.run("SELECT count(*) FROM partitions WHERE start>=? AND end<=?", (str(start), str(end))).require_one()[0]
            == 1
        )
    assert not any(p.start == start and p.end == end for p in index.partitions(("sse",), NOW + timedelta(minutes=16)))
    reopened = index.partitions(("sse",), NOW + timedelta(days=1))
    assert sum(p.start == start and p.end == end for p in reopened) == 1


def test_old_overlapping_parent_and_child_cursors_are_consumed_once(tmp_path):
    index = fixtures._index(tmp_path)
    index.partitions(("sse",), NOW)
    parent = DisclosurePartition("sse", NOW.date() - timedelta(days=1), NOW.date())
    child = DisclosurePartition("sse", NOW.date(), NOW.date())
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "INSERT INTO partitions(market,start,end) VALUES(?,?,?)",
            (parent.market, parent.start.isoformat(), parent.end.isoformat()),
        )
    index.commit_page(parent, DisclosurePage((fixtures._disclosure("parent"),), 2, True), NOW)
    index.commit_page(child, DisclosurePage((fixtures._disclosure("head"),), 2, True), NOW)

    class GrowingRange(fixtures.Sources):
        def fetch(self, query, *args, **kwargs):
            if isinstance(query, DisclosurePartition) and query.end == NOW.date():
                self.calls.append(query)
                if query.start < query.end:
                    return DisclosurePage((), 3001, True)
                assert query.page == 2
                return DisclosurePage((fixtures._disclosure("tail"),), 2, False)
            return super().fetch(query, *args, **kwargs)

    source = GrowingRange()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW
    )
    requests = Counter((p.start, p.end, p.page) for p in source.calls if isinstance(p, DisclosurePartition))
    assert requests[(child.start, child.end, 2)] == 1
    assert not result.needs_retry
    assert not SQLiteCompanyEvidenceIndex(index.path).partitions(("sse",), NOW)
    assert SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_state is CompanyDataState.COMPLETE


def test_split_descendants_survive_restarts_and_old_month_audits(tmp_path):
    index = fixtures._index(tmp_path)
    original = next(p for p in index.partitions(("sse",), NOW) if p.start < p.end)
    children = index.split_partition(original, NOW)
    index.commit_page(children[0], DisclosurePage((fixtures._disclosure(),), 2, True), NOW)
    restarted = SQLiteCompanyEvidenceIndex(index.path)
    restarted.initialize()
    for later in (NOW, NOW + timedelta(days=1), NOW + timedelta(days=20)):
        resumed = restarted.partitions(("sse",), later)
        assert not any((p.start, p.end) == (original.start, original.end) for p in resumed)
        first = next(p for p in resumed if (p.start, p.end) == (children[0].start, children[0].end))
        assert first.page == 2 and first.received_rows == 1


def test_existing_page_one_precedes_new_calendar_day_and_rechecks(tmp_path):
    index = fixtures._index(tmp_path)
    planned = index.partitions(("sse",), NOW)
    old = min(planned, key=lambda item: item.start)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE partitions SET complete=1,checked_at=?", (NOW.isoformat(),))
        connection.run(
            "UPDATE partitions SET complete=0 WHERE start=? AND end=?", (old.start.isoformat(), old.end.isoformat())
        )
    pending = index.partitions(("sse",), NOW + timedelta(days=1))
    assert pending[0].end == NOW.date() + timedelta(days=1)
    assert old in pending
    assert any(item.start == item.end == NOW.date() + timedelta(days=1) for item in pending)


def test_already_deferred_child_is_not_retried_when_parent_splits(tmp_path):
    index = fixtures._index(tmp_path)
    index.partitions(("sse",), NOW)
    parent = DisclosurePartition("sse", NOW.date() - timedelta(days=1), NOW.date())
    child = DisclosurePartition("sse", NOW.date(), NOW.date())
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "INSERT INTO partitions(market,start,end) VALUES(?,?,?)",
            (parent.market, parent.start.isoformat(), parent.end.isoformat()),
        )
    index.commit_page(child, DisclosurePage((fixtures._disclosure("head"),), 2, True), NOW)

    class Source(fixtures.Sources):
        def fetch(self, query, *args, **kwargs):
            if isinstance(query, DisclosurePartition) and query.end == NOW.date():
                self.calls.append(query)
                if query.start < query.end:
                    return DisclosurePage((), 3001, True)
                return DisclosurePage((fixtures._disclosure("head"),), 2, False)
            return super().fetch(query, *args, **kwargs)

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW
    )
    requests = [p for p in source.calls if isinstance(p, DisclosurePartition) and p.start == p.end == NOW.date()]
    assert len(requests) == 1 and requests[0].page == 2
    assert result.pending_partitions == 1
    assert (
        SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_state is not CompanyDataState.COMPLETE
    )


def test_duplicate_multiday_page_splits_and_converges(tmp_path):
    index = fixtures._index(tmp_path)
    parent = next(p for p in index.partitions(("sse",), NOW) if (p.end - p.start).days > 2)

    class DuplicateRange(fixtures.Sources):
        def fetch(self, query, *args, **kwargs):
            if query == parent:
                self.calls.append(query)
                item = fixtures._disclosure(when=NOW.replace(day=parent.start.day))
                return DisclosurePage((item, item), 2, False)
            return super().fetch(query, *args, **kwargs)

    source = DuplicateRange()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW
    )
    assert not result.needs_retry
    assert not index.partitions(("sse",), NOW)


def test_partial_recheck_cannot_borrow_old_complete_coverage(tmp_path):
    index = fixtures._index(tmp_path)
    source = fixtures.Sources()
    fixtures._sync_company_evidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    later = NOW + timedelta(minutes=30)
    pending = index.partitions(("sse",), later)
    index.commit_page(pending[0], DisclosurePage((fixtures._disclosure(when=later),), 2, True), later)
    index.publish(("600001",), later)
    snapshot = SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0]
    assert snapshot.announcement_state is not CompanyDataState.COMPLETE
    assert any(item.scope == "risk" and item.reason_code == "pending" for item in snapshot.review_reasons)
    assert snapshot.announcement_coverage_end == later.date()


def test_oldest_calendar_day_risk_body_is_queued_and_published(tmp_path):
    index = fixtures._index(tmp_path)
    source = fixtures.Sources()
    deps = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    oldest = min(index.partitions(("sse",), NOW), key=lambda p: p.start)
    fixtures._sync_company_evidence(deps, ("600001",), NOW)
    risk = fixtures._disclosure(title="关于收到立案告知书的公告", when=NOW - timedelta(days=365, hours=1))
    index.commit_page(oldest, DisclosurePage((risk,), 1, False), NOW)
    assert index.pending_documents(("600001",), NOW) == (risk,)

    class Documents(fixtures.Sources):
        def document(self, item, **kwargs):
            assert item == risk
            return "本公司收到证监会立案告知书。"

    source = Documents()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW
    )
    assert result.pending_documents == 0 and not result.needs_retry
    assert SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_state is CompanyDataState.COMPLETE


@pytest.mark.parametrize("recovery", ["expiry", "digest", "configuration"])
def test_failed_body_is_deferred_across_restart_and_retried_on_recovery(tmp_path, recovery):
    index = fixtures._index(tmp_path)
    source = fixtures.Sources()
    fixtures._sync_company_evidence(CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW)
    day = DisclosurePartition("sse", NOW.date(), NOW.date())
    item = fixtures._disclosure(title="关于收到立案告知书的公告")
    index.commit_page(day, DisclosurePage((item,), 1, False), NOW)

    class Documents(fixtures.Sources):
        attempts = 0
        blocked = True

        def document(self, item, **kwargs):
            self.attempts += 1
            if self.blocked:
                raise ValueError("disclosure_document_oversized")
            return "本公司收到证监会立案告知书。"

    source = Documents()
    events = []
    configuration = replace(fixtures._TEST_COMPANY_CONFIGURATION, document_failure_retry_seconds=10)
    deps = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    first = fixtures._sync_company_evidence(deps, ("600001",), NOW, configuration=configuration)
    index = SQLiteCompanyEvidenceIndex(index.path)
    deps = replace(deps, index=index)
    second = fixtures._sync_company_evidence(deps, ("600001",), NOW, configuration=configuration)
    assert source.attempts == 1 and first.pending_documents == second.pending_documents == 1
    final_body = [event for event in events if event.component == "risk_documents"][-1]
    assert final_body.deferred_documents == 1 and final_body.attempted_documents == 0
    assert (
        SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_state is not CompanyDataState.COMPLETE
    )
    now = NOW
    if recovery == "expiry":
        now += timedelta(seconds=10)
    elif recovery == "digest":
        index.commit_page(day, DisclosurePage((replace(item, digest="changed"),), 1, False), now)
    else:
        configuration = replace(configuration, document_parse_timeout_seconds=20)
    source.blocked = False
    third = fixtures._sync_company_evidence(deps, ("600001",), now, configuration=configuration)
    assert source.attempts == 2 and not third.needs_retry
    assert SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0].announcement_state is CompanyDataState.COMPLETE


def test_retry_schema_extension_preserves_old_cursors_and_publication(tmp_path):
    index = fixtures._index(tmp_path)
    part = index.partitions(("sse",), NOW)[0]
    index.commit_page(part, DisclosurePage((fixtures._disclosure(),), 2, True), NOW)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.path)
    before = reader.read(("600001",))
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("DROP TABLE document_retries")  # Original deployed schema, isolated fixture only.
    index.initialize()
    index.initialize()
    resumed = index.partitions(("sse",), NOW)[0]
    assert resumed.page == 2 and resumed.received_rows == 1
    assert reader.read(("600001",)) == before
