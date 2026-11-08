"""Cancellation interrupts company I/O without discarding durable pages or publication."""

from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_sources import Session
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.application.company_documents import CompanyDocumentRun
from trader.download.domain.company_evidence import DisclosurePage, DisclosurePartition
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture as CompanyDocumentFiles, SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex
from tests.helpers.download.company_resources import EastmoneyCompanySourceFixture as EastmoneyCompanySource
from trader.infra.http_contracts import HttpCancelled
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction


def test_cancellation_before_initialization_does_not_create_database(tmp_path):
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "evidence.sqlite3",
        facts_path=(tmp_path / "evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "evidence.sqlite3").parent / "text"),
        cancel_requested=lambda: True,
    )
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        index.initialize()
    assert not index.path.exists()


def test_cancelled_sync_before_initialization_reports_unknown_inventory_without_io(tmp_path):
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "evidence.sqlite3",
        facts_path=tmp_path / "company_facts.sqlite3",
        documents=CompanyDocumentFiles(tmp_path / "text"),
        cancel_requested=lambda: True,
    )
    source = fixtures.Sources()
    events = []
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(
            index, source, source, lambda: 0, progress=events.append, cancel_requested=lambda: True
        ),
        ("600001",),
        fixtures.NOW,
    )
    assert result.failures == ("task:cancelled",) and result.needs_retry
    assert result.pending_partitions is result.pending_documents is None
    assert result.ocr_documents is result.unextractable_documents is None
    assert result.published_codes == 0 and source.calls == [] and not index.path.exists()
    assert events[-1].pending is events[-1].pending_documents is None
    assert events[-1].workload is None


def test_cancelled_sync_keeps_pending_inventory_unknown_and_does_not_query_after_cancel(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("pending-case", "关于收到立案告知书的公告")
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    cancelled = Event()
    inventory = index.document_workload

    def checked_inventory(*args, **kwargs):
        assert not cancelled.is_set(), "cancelled summary must not reopen the inventory database"
        return inventory(*args, **kwargs)

    monkeypatch.setattr(index, "document_workload", checked_inventory)

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            cancelled.set()
            raise HttpCancelled("http_cancelled")

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        item.codes,
        fixtures.NOW,
    )
    assert result.failures == ("task:cancelled",)
    assert result.pending_partitions is result.pending_documents is None
    assert result.ocr_documents is result.unextractable_documents is None
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT body_checked FROM disclosures WHERE source_key=?", (item.identity,)).require_one()[0] == 0


def test_cancellation_during_document_batch_preserves_commits_without_final_inventory_query(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = fixtures._disclosure("accepted-case", "关于收到立案告知书的公告")
    partition = DisclosurePartition("sse", item.published_at.date(), item.published_at.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    cancelled = Event()
    inventory = index.document_workload

    def checked_inventory(*args, **kwargs):
        assert not cancelled.is_set(), "cancelled document batch must not query its final inventory"
        return inventory(*args, **kwargs)

    monkeypatch.setattr(index, "document_workload", checked_inventory)

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition):
                present = query.market == "sse" and query.start <= item.published_at.date() <= query.end
                return DisclosurePage((item,) if present else (), int(present), False)
            return super().fetch(query, **kwargs)

        def extract_document(self, disclosure, *, route="standard"):
            cancelled.set()
            return fixtures._extraction("本公司收到证监会立案告知书〔2026〕调查1号，决定立案。")

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        item.codes,
        fixtures.NOW,
    )
    assert result.failures == ("task:cancelled",) and result.published_codes == 1
    assert result.pending_partitions is result.pending_documents is None
    assert result.changed_rows >= 1
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run(
            "SELECT body_checked,body_retained FROM disclosures WHERE source_key=?", (item.identity,)
        ).require_one()) == (1, 1)


def test_cancellation_during_final_inventory_read_returns_unknown_counts_and_keeps_published_count(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    cancelled = Event()
    inventory = CompanyDocumentRun.workload

    def interrupted_inventory(documents, *, include_attempted=False):
        if include_attempted:
            cancelled.set()
            raise InterruptedError("company_sync_cancelled")
        return inventory(documents, include_attempted=include_attempted)

    monkeypatch.setattr(CompanyDocumentRun, "workload", interrupted_inventory)
    source = fixtures.Sources()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        ("600001",),
        fixtures.NOW,
    )
    assert result.failures == ("task:cancelled",) and result.published_codes == 1
    assert result.pending_partitions is result.pending_documents is None


def test_long_query_cancellation_rolls_back_only_uncommitted_changes(tmp_path):
    cancelled = Event()
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "evidence.sqlite3",
        facts_path=(tmp_path / "evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "evidence.sqlite3").parent / "text"),
        cancel_requested=cancelled.is_set,
    )
    index.initialize()
    day = fixtures.NOW.date()
    partition = DisclosurePartition("sse", day, day)
    with index._connect() as connection:
        connection.run("INSERT INTO partitions(market,start_date,end_date) VALUES('sse',?,?)", (str(day), str(day)))
    index.commit_page(partition, DisclosurePage((fixtures._disclosure(),), 2, True), fixtures.NOW)
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        with index._connect() as connection, SqliteTransaction(connection):
            connection.run("UPDATE partitions SET next_page=999")
            cancelled.set()
            connection.run(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000) SELECT sum(x) FROM n"
            ).fetchall()
    with SqliteSession(index.path) as connection:
        assert tuple(connection.run("SELECT next_page,received_rows,failure FROM partitions").require_one()) == (
            2,
            1,
            "",
        )
        assert connection.run("SELECT count(*) FROM disclosures").require_one()[0] == 1
    # A new invocation can continue from the committed cursor.
    cancelled.clear()
    resumed = index.partitions(("sse",), fixtures.NOW)
    assert next(item for item in resumed if item.start == day).page == 2


def test_cancelled_publication_keeps_previous_projection(tmp_path, monkeypatch):
    from trader.download.infra import company_evidence_index as publication

    cancelled = Event()
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "evidence.sqlite3",
        facts_path=(tmp_path / "evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "evidence.sqlite3").parent / "text"),
        cancel_requested=cancelled.is_set,
    )
    index.initialize()
    index.publish(("600001",), fixtures.NOW)
    with SqliteSession(index.facts_path) as connection:
        before = tuple(connection.run("SELECT * FROM company_publication").require_one())
    encode = publication.CompileCompanyFacts

    def cancel_during_build(snapshot, *args, **kwargs):
        result = encode(snapshot, *args, **kwargs)
        cancelled.set()
        return result

    monkeypatch.setattr(publication, "CompileCompanyFacts", cancel_during_build)
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        index.publish(("600001", "600002"), fixtures.NOW)
    with SqliteSession(index.facts_path) as connection:
        assert tuple(connection.run("SELECT * FROM company_publication").require_one()) == before
        assert connection.run("SELECT count(*) FROM company_facts").require_one()[0] == 1


@pytest.mark.parametrize("during_request", (False, True))
@pytest.mark.parametrize("component", ("pledge", "news"))
def test_structured_request_receives_cancellation_and_discards_late_response(during_request, component):
    cancelled = Event()

    class Transport(Session):
        def get(self, url, **kwargs):
            assert kwargs["cancel_requested"] == cancelled.is_set
            cancelled.set()
            return super().get(url, **kwargs)

    session = Transport({})
    source = EastmoneyCompanySource(session, monotonic=lambda: 0, cancel_requested=cancelled.is_set)
    if not during_request:
        cancelled.set()
    with pytest.raises(HttpCancelled):
        if component == "pledge":
            source.pledge_day(fixtures.NOW)
        else:
            source.news("600001", observed_at=fixtures.NOW)
    assert len(session.requests) == int(during_request)


@pytest.mark.parametrize("component", ("financial", "news"))
def test_cancelled_component_does_not_persist_source_failure(tmp_path, monkeypatch, component):
    index = fixtures._index(tmp_path)
    cancelled = Event()
    failures = []
    monkeypatch.setattr(index, "component_failure", lambda *args: failures.append(args))

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if component == "financial":
                cancelled.set()
                raise HttpCancelled("http_cancelled")
            return super().fetch(query, **kwargs)

        def news(self, code, **kwargs):
            cancelled.set()
            raise HttpCancelled("http_cancelled")

    source = Source()
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        ("600001",),
        fixtures.NOW,
        news_codes=("600001",),
    )
    assert result.failures == ("task:cancelled",) and not failures


@pytest.mark.parametrize("phase", ("prepare", "publish_fields", "publish_contract"))
def test_material_read_cancellation_is_propagated_before_missing_evidence_recovery(tmp_path, monkeypatch, phase):
    from tests.helpers.download.company import company_field_recovery as field_fixtures
    from trader.infra import regular_file

    if phase == "publish_contract":
        index = fixtures._index(tmp_path)
        item = fixtures._disclosure("retained-contract", "关于签订重大合同的公告")
        body = "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）。" + field_fixtures._DETAIL
        field_fixtures._put(index, item, body)
        index.publish(item.codes, fixtures.NOW)
        digest = index._documents.write(body)
        with SqliteSession(index.path) as collected, SqliteTransaction(collected):
            collected.run("UPDATE disclosures SET body_retained=1,body_hash=?", (digest,))
            collected.run("DELETE FROM document_contract_link_rules")
    else:
        index, item = field_fixtures._legacy_repurchase_template(tmp_path)
    cancelled = Event()
    index = SQLiteCompanyEvidenceIndex(
        index.path, facts_path=index.facts_path, documents=index._documents,
        cancel_requested=cancelled.is_set,
    )
    before = index.facts_path.read_bytes()
    original_document_read = index._documents.read
    original_stream_read = regular_file._RegularFileReader.read
    original_check = index._check_cancelled
    reading = False
    interruptions = []

    def document_read(digest, *, check=None):
        nonlocal reading
        reading = True
        try:
            return original_document_read(digest, check=check)
        finally:
            reading = False

    def stream_read(stream, size=-1):
        content = original_stream_read(stream, size)
        if reading and content:
            cancelled.set()
        return content

    def check_cancelled():
        try:
            original_check()
        except InterruptedError as exc:
            if reading:
                interruptions.append(exc)
            raise

    monkeypatch.setattr(index._documents, "read", document_read)
    monkeypatch.setattr(regular_file._RegularFileReader, "read", stream_read)
    monkeypatch.setattr(index, "_check_cancelled", check_cancelled)
    operation = index.prepare_publication if phase == "prepare" else index.publish
    with pytest.raises(InterruptedError, match="company_sync_cancelled") as failure:
        operation(item.codes, fixtures.NOW)
    assert cancelled.is_set() and len(interruptions) == 1
    assert failure.value is interruptions[0], "The material cancellation must escape its consumer unchanged"
    assert index.facts_path.read_bytes() == before
