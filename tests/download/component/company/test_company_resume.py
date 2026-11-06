"""Company restart, durable failure and published coverage regression scenarios."""

from collections import Counter
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import (
    CompanyFinancial,
    CompanyListing,
    DisclosurePage,
    DisclosurePartition,
    StructuredCompanyPage,
    StructuredCompanyQuery,
)
from trader.download.infra.company_raw_files import CompanyRawFiles
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from tests.helpers.shared.configuration import (
    SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex,
    CompanyDocumentFilesFixture as CompanyDocumentFiles,
    SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader,
)

NOW = fixtures.NOW


def _local_backlog(tmp_path, count):
    raw = CompanyRawFiles(tmp_path, capacity=10000000, analysis_reserve=1000000, free_reserve=1)
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "company_evidence.sqlite3",
        facts_path=tmp_path / "company_facts.sqlite3",
        documents=CompanyDocumentFiles(tmp_path / "text"),
        raw_files=raw,
        listings=(CompanyListing("600001", NOW.date() - timedelta(days=365)),),
    )
    index.initialize()
    partition = next(p for p in index.partitions(("sse",), NOW) if p.start == p.end == NOW.date())
    items = tuple(
        fixtures._disclosure(str(i), "关于收到立案告知书的公告", when=NOW - timedelta(minutes=i))
        for i in range(count)
    )
    index.commit_page(partition, DisclosurePage(items, count, False), NOW)
    # One unfinished directory day is enough to exercise batch interleaving.
    day = (NOW - timedelta(days=1)).date().isoformat()
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE partitions SET is_complete=1,checked_at=?", (NOW.isoformat(),))
        connection.run("UPDATE partitions SET is_complete=0 WHERE start_date=? AND end_date=?", (day, day))
        connection.run(
            "INSERT OR REPLACE INTO coverage VALUES('sse',?,?,?)",
            ((NOW.date() - timedelta(days=365)).isoformat(), NOW.date().isoformat(), NOW.isoformat()),
        )
    for item in items:
        directory = raw.directory(item)
        directory.mkdir(parents=True)
        partial = directory / "download.part"
        partial.write_bytes(b"%PDF-isolated-local-backlog-fixture")
        raw.complete(item, partial)
        index.document_stage(item, "downloaded")
    return index, raw, items


@pytest.mark.parametrize("structured_failure", (False, True))
def test_latest_structured_data_precedes_one_local_batch_then_directory_discovery(tmp_path, structured_failure):
    index, raw, items = _local_backlog(tmp_path, 3)
    order = []
    financial = CompanyFinancial("600001", NOW.date() - timedelta(days=30), NOW, 1, 2, 3, 4, 5, 6, 7, 8)

    class Source(fixtures.Sources):
        def fetch(self, query, *args, **kwargs):
            if isinstance(query, StructuredCompanyQuery):
                order.append(query.component)
                if structured_failure and query.component == "financial":
                    raise TimeoutError("fixture")
                if query.component == "financial":
                    return StructuredCompanyPage("financial", query.page, 1, 1, financials=(financial,))
            else:
                assert query.start >= NOW.date() - timedelta(days=365)
                order.append("announcements")
            return super().fetch(query, *args, **kwargs)

        def document_ready(self, item):
            return raw.local(item) is not None

        def download_document(self, item):
            pytest.fail("retained local PDF was downloaded again")

        def extract_document(self, item, **kwargs):
            assert raw.local(item) is not None
            snapshot = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0]
            assert snapshot.financial == (None if structured_failure else financial)
            order.append(("local", item.identity))
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0),
        ("600001",),
        NOW,
        configuration=replace(fixtures._TEST_COMPANY_CONFIGURATION, document_batch_size=1, directory_batch_pages=1),
    )
    assert order[:3] == ["financial", "pledge", "unlock"]
    assert order[3:5] == [("local", items[0].identity), "announcements"]
    assert Counter(item[1] for item in order if isinstance(item, tuple)) == Counter(item.identity for item in items)
    assert result.needs_retry is structured_failure and result.pending_documents == 0
    snapshot = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0]
    assert snapshot.risk_events
    assert ("financial:timeout" in result.failures) is structured_failure
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT SUM(body_checked) FROM disclosures").require_one()[0] == len(items)


@pytest.mark.parametrize("phase", ("structured", "local"))
@pytest.mark.parametrize("fill_missing", (False, True))
def test_structured_first_local_recovery_resumes_after_cancellation(tmp_path, phase, fill_missing):
    from trader.download.application.company_evidence import SyncCompanyEvidence
    from tests.helpers.shared.configuration import CompanyEvidenceRequestFixture

    index, raw, items = _local_backlog(tmp_path, 2)
    cancelled = False
    resumed = False
    analyzed = []

    def progress(event):
        nonlocal cancelled
        if not resumed and phase == "structured" and event.component == "pledge" and event.status == "running":
            cancelled = True

    class Source(fixtures.Sources):
        def document_ready(self, item):
            return raw.local(item) is not None

        def download_document(self, item):
            pytest.fail("retained local PDF was downloaded again")

        def extract_document(self, item, **kwargs):
            nonlocal cancelled
            analyzed.append(item.identity)
            if not resumed and phase == "local":
                cancelled = True
            return fixtures._extraction("本公司收到中国证监会立案告知书。")

    source = Source()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0, progress, lambda: cancelled)
    configuration = replace(fixtures._TEST_COMPANY_CONFIGURATION, document_batch_size=1)
    request = CompanyEvidenceRequestFixture(("600001",), NOW, configuration, fill_missing=fill_missing)
    first = SyncCompanyEvidence(dependencies, request)
    assert "task:cancelled" in first.failures
    assert first.published_codes == int(phase == "local")
    assert analyzed == ([items[0].identity] if phase == "local" else [])
    assert all(raw.local(item) is not None for item in items if item.identity not in analyzed)
    cancelled, resumed = False, True
    second = SyncCompanyEvidence(dependencies, request)
    assert not second.needs_retry and second.pending_documents == 0
    assert Counter(analyzed) == Counter(item.identity for item in items)
    assert SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].risk_events


def test_document_inventory_switches_progress_before_reading_after_announcement_failure(tmp_path):
    events = []
    inventories = []

    class Index(SQLiteCompanyEvidenceIndex):
        def pending_documents(self, codes, observed_at, **kwargs):
            if not inventories:
                assert events[-1].component == "announcements" and events[-1].status == "deferred"
            inventories.append(True)
            return super().pending_documents(codes, observed_at, **kwargs)

    class Source(fixtures.Sources):
        def fetch(self, query, *args, **kwargs):
            if isinstance(query, DisclosurePartition):
                raise TimeoutError("fixture")
            return super().fetch(query, *args, **kwargs)

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(
            Index(
                tmp_path / "company.sqlite3",
                facts_path=tmp_path / "company_facts.sqlite3",
                documents=CompanyDocumentFiles(tmp_path / "text"),
            ),
            source,
            source,
            lambda: 0,
            events.append,
        ),
        ("600001",),
        NOW,
    )
    assert inventories and result.needs_retry and result.published_codes == 1


def test_recent_day_planning_never_duplicates_a_completed_wider_owner(tmp_path):
    index = fixtures._index(tmp_path)
    index.partitions(("sse",), NOW)
    start, end = NOW.date() - timedelta(days=8), NOW.date() - timedelta(days=1)
    with SqliteSession(index.path) as c, SqliteTransaction(c):
        c.run("DELETE FROM partitions WHERE start_date>=? AND end_date<=?", (str(start), str(end)))
        c.run(
            "INSERT INTO partitions(market,start_date,end_date,is_complete,checked_at) VALUES('sse',?,?,1,?)",
            (str(start), str(end), NOW.isoformat()),
        )
    index.partitions(("sse",), NOW)
    with SqliteSession(index.path) as c:
        assert (
            c.run("SELECT count(*) FROM partitions WHERE start_date>=? AND end_date<=?", (str(start), str(end))).require_one()[0]
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
            "INSERT INTO partitions(market,start_date,end_date) VALUES(?,?,?)",
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
    assert not SQLiteCompanyEvidenceIndex(
        index.path, facts_path=index.facts_path, documents=CompanyDocumentFiles(index.path.parent / "text")
    ).partitions(("sse",), NOW)
    assert (
        SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].announcement_state
        is CompanyDataState.COMPLETE
    )


def test_split_descendants_survive_restarts_and_old_month_audits(tmp_path):
    index = fixtures._index(tmp_path)
    original = next(p for p in index.partitions(("sse",), NOW) if p.start < p.end)
    children = index.split_partition(original, NOW)
    index.commit_page(children[0], DisclosurePage((fixtures._disclosure(),), 2, True), NOW)
    restarted = SQLiteCompanyEvidenceIndex(
        index.path, facts_path=index.facts_path, documents=CompanyDocumentFiles(index.path.parent / "text")
    )
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
        connection.run("UPDATE partitions SET is_complete=1,checked_at=?", (NOW.isoformat(),))
        connection.run(
            "UPDATE partitions SET is_complete=0 WHERE start_date=? AND end_date=?", (old.start.isoformat(), old.end.isoformat())
        )
    pending = index.partitions(("sse",), NOW + timedelta(days=1))
    assert pending[0].end == NOW.date() + timedelta(days=1)
    assert replace(old, start=old.start + timedelta(days=1)) in pending
    assert all(item.start >= NOW.date() + timedelta(days=1) - timedelta(days=365) for item in pending)
    assert any(item.start == item.end == NOW.date() + timedelta(days=1) for item in pending)


def test_already_deferred_child_is_not_retried_when_parent_splits(tmp_path):
    index = fixtures._index(tmp_path)
    index.partitions(("sse",), NOW)
    parent = DisclosurePartition("sse", NOW.date() - timedelta(days=1), NOW.date())
    child = DisclosurePartition("sse", NOW.date(), NOW.date())
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "INSERT INTO partitions(market,start_date,end_date) VALUES(?,?,?)",
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
        SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].announcement_state
        is not CompanyDataState.COMPLETE
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
    snapshot = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0]
    assert snapshot.announcement_state is not CompanyDataState.COMPLETE
    assert any(item.scope == "risk" and item.reason_code == "source_task_pending" for item in snapshot.review_reasons)
    assert snapshot.announcement_coverage_end == later.date()


def test_oldest_calendar_day_risk_body_is_queued_and_published(tmp_path):
    index = fixtures._index(tmp_path)
    source = fixtures.Sources()
    deps = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    oldest = min(index.partitions(("sse",), NOW), key=lambda p: p.start)
    fixtures._sync_company_evidence(deps, ("600001",), NOW)
    risk = fixtures._disclosure(title="关于收到立案告知书的公告", when=NOW - timedelta(days=365, hours=1))
    index.commit_page(oldest, DisclosurePage((risk,), 1, False), NOW)
    assert tuple(task.disclosure for task in index.pending_documents(("600001",), NOW, policies=fixtures._TEST_DOCUMENT_POLICIES)) == (risk,)

    class Documents(fixtures.Sources):
        def document(self, item, **kwargs):
            assert item == risk
            return fixtures._extraction("本公司收到证监会立案告知书。")

    source = Documents()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), ("600001",), NOW
    )
    assert result.pending_documents == 0 and not result.needs_retry
    assert (
        SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].announcement_state
        is CompanyDataState.COMPLETE
    )


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
            return fixtures._extraction("本公司收到证监会立案告知书。")

    source = Documents()
    events = []
    configuration = replace(fixtures._TEST_COMPANY_CONFIGURATION, document_failure_retry_seconds=10)
    deps = CompanyEvidenceDependencies(index, source, source, lambda: 0, events.append)
    first = fixtures._sync_company_evidence(deps, ("600001",), NOW, configuration=configuration)
    index = SQLiteCompanyEvidenceIndex(
        index.path,
        facts_path=index.facts_path,
        documents=CompanyDocumentFiles(index.path.parent / "text"),
        listings=(CompanyListing("600001", NOW.date() - timedelta(days=365)),),
    )
    deps = replace(deps, index=index)
    second = fixtures._sync_company_evidence(deps, ("600001",), NOW, configuration=configuration)
    assert source.attempts == 1 and first.pending_documents == second.pending_documents == 0
    final_body = [event for event in events if event.component == "risk_documents"][-1]
    assert final_body.unextractable_documents == 1 and final_body.attempted_documents == 0
    assert (
        SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].announcement_state
        is not CompanyDataState.COMPLETE
    )
    now = NOW
    if recovery == "expiry":
        now += timedelta(seconds=10)
    elif recovery == "digest":
        index.commit_page(day, DisclosurePage((replace(item, digest="changed"),), 1, False), now)
    else:
        expanded = configuration.extended_document_limits
        configuration = replace(
            configuration,
            extended_document_limits=replace(expanded, max_pages=expanded.max_pages - 1),
        )
    source.blocked = False
    third = fixtures._sync_company_evidence(deps, ("600001",), now, configuration=configuration)
    if recovery == "expiry":
        assert source.attempts == 1 and not third.needs_retry
    else:
        assert source.attempts == 2 and not third.needs_retry
        assert (
            SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0].announcement_state
            is CompanyDataState.COMPLETE
        )


def test_incomplete_retry_schema_is_rejected_without_migration(tmp_path):
    index = fixtures._index(tmp_path)
    part = index.partitions(("sse",), NOW)[0]
    index.commit_page(part, DisclosurePage((fixtures._disclosure(),), 2, True), NOW)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    before = reader.read(("600001",))
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("DROP TABLE document_retries")  # Original deployed schema, isolated fixture only.
        connection.run(
            "CREATE TABLE document_retries(disclosure_id INTEGER PRIMARY KEY REFERENCES disclosures(id) ON DELETE CASCADE,"
            "record_hash TEXT NOT NULL,retry_policy_id TEXT NOT NULL,failure TEXT NOT NULL,attempted_at TEXT NOT NULL,"
            "retry_at TEXT NOT NULL,attempts INTEGER NOT NULL)"
        )
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable"):
        index.initialize()
    assert reader.read(("600001",)) == before
    with SqliteSession(index.path) as connection:
        assert "payload_hash" not in {row[1] for row in connection.run("PRAGMA table_info(document_retries)")}
        assert connection.run(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_extractions'"
        ).fetchone()


def test_company_noop_publish_keeps_identity_and_closes_wal_before_sealing(tmp_path):
    from tests.helpers.shared.configuration import SqlitePolicyFixture
    from trader.infra.sqlite_publication import VerifySqliteFile

    index = fixtures._index(tmp_path)
    part = index.partitions(("sse",), NOW)[0]
    index.commit_page(part, DisclosurePage((fixtures._disclosure(),), 2, True), NOW)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    identity = reader.publication_version()
    first = VerifySqliteFile(index.facts_path, 3, sqlite_policy=SqlitePolicyFixture())
    assert first.fingerprint.wal_size == 0

    index.publish(("600001",), NOW)
    index.initialize()

    assert reader.publication_version() == identity
    assert VerifySqliteFile(index.facts_path, 3, sqlite_policy=SqlitePolicyFixture()).fingerprint == first.fingerprint


def test_company_generation_tracks_sources_and_receipts_with_identical_facts(tmp_path):
    from tests.helpers.shared.configuration import CompanyEventPolicyFixture, SqlitePolicyFixture
    from trader.download.infra.company_facts_index import (
        CompanyInputReceipt, ReadCompanyPublicationIdentity, SQLiteCompanyFactsIndex,
    )

    index = fixtures._index(tmp_path / "input")
    part = index.partitions(("sse",), NOW)[0]
    item = fixtures._disclosure("notice", "关于收到立案告知书的公告")
    index.commit_page(part, DisclosurePage((item,), 1, False), NOW)
    index.commit_document(item, fixtures._extraction("本公司收到证监会立案告知书。"), NOW)
    index.publish(("600001",), NOW)
    fact = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0]
    assert fact.risk_events[0].evidence_ids == ("notice",)
    generations = []
    for name, source in (("first", "cninfo"), ("same", "cninfo"), ("changed-source", "sse")):
        writer = SQLiteCompanyFactsIndex(
            tmp_path / name / "facts.sqlite3", event_policy=CompanyEventPolicyFixture(),
            sqlite_policy=SqlitePolicyFixture(),
        )
        writer.initialize()
        writer.publish((fact,), {"notice": source}, None, NOW)
        with SqliteSession(writer.path) as connection:
            parent = ReadCompanyPublicationIdentity(connection)
        assert parent is not None
        generations.append(parent.content_hash)
        receipt = CompanyInputReceipt("cninfo", "notice", "600001", "revision", writer.extraction_hash, None, NOW)
        writer.publish((), {"notice": source}, parent, NOW, receipts=(receipt,))
        with SqliteSession(writer.path) as connection:
            after_receipt = ReadCompanyPublicationIdentity(connection)
        assert after_receipt is not None and after_receipt.content_hash != parent.content_hash
        writer.publish((), {"notice": source}, after_receipt, NOW, receipts=(receipt,))
        with SqliteSession(writer.path) as connection:
            assert ReadCompanyPublicationIdentity(connection) == after_receipt
    assert generations[0] == generations[1]
    assert generations[0] != generations[2]


@pytest.mark.parametrize("leftover", ("empty_file", "rolled_back_schema"))
def test_company_first_initialization_recovers_only_empty_leftover(tmp_path, leftover):
    from tests.helpers.shared.configuration import CompanyEventPolicyFixture, SqlitePolicyFixture
    from trader.download.infra.company_facts_index import SQLiteCompanyFactsIndex

    path = tmp_path / "facts.sqlite3"
    if leftover == "empty_file":
        path.touch()
    else:
        with SqliteSession(path) as connection:
            with pytest.raises(RuntimeError), SqliteTransaction(connection):
                connection.run("CREATE TABLE interrupted_fixture(value INTEGER)")
                raise RuntimeError("first schema creation interrupted")
    writer = SQLiteCompanyFactsIndex(path, event_policy=CompanyEventPolicyFixture(), sqlite_policy=SqlitePolicyFixture())

    writer.initialize()

    with SqliteSession(path) as connection:
        assert connection.run("PRAGMA user_version").require_one()[0] == 3
        assert connection.run("SELECT COUNT(*) FROM company_facts").require_one()[0] == 0
