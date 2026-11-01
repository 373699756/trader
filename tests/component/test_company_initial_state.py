"""Initial company risk state needs history provenance, including cross-year active cases."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.component import test_company_evidence as fixtures
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import CompanyListing, Disclosure, DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from tests.helpers.configuration import (
    CompanyEvidenceRequestFixture as CompanyEvidenceRequest,
    SQLiteCompanyEvidenceIndexFixture as SQLiteCompanyEvidenceIndex,
    CompanyDocumentFilesFixture as CompanyDocumentFiles,
    SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader,
)
from trader.download.application.company_evidence import CompanyEvidenceDependencies, SyncCompanyEvidence
from trader.download.application.sync_data import SyncResult, SyncTaskResult
from trader.download.entrypoints.sync_projection import ProjectSyncResult

_AT = datetime(2026, 10, 27, 10, tzinfo=ZoneInfo("Asia/Shanghai"))


def _coverage(index, start):
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "INSERT OR REPLACE INTO coverage VALUES('sse',?,?,?)",
            (start.isoformat(), _AT.date().isoformat(), _AT.isoformat()),
        )


def _old_document(index, body):
    old = _AT - timedelta(days=400)
    partition = DisclosurePartition("sse", old.date(), old.date())
    document = Disclosure("cninfo", "old-case", ("600001",), "收到立案告知书", old, "https://example.test/old", "old")
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO partitions(market,start_date,end_date) VALUES('sse',?,?)", (str(old.date()), str(old.date())))
    index.commit_page(partition, DisclosurePage((document,), 1, False), _AT)
    if body is not None:
        index.commit_document(document, fixtures._extraction(body), _AT)
    return old


def test_first_publication_retains_old_unresolved_case_and_exposes_unverified_initial_state(tmp_path):
    listed = _AT.date() - timedelta(days=1000)
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=(CompanyListing("600001", listed),),
    )
    index.initialize()
    old = _old_document(index, "本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    first = reader.read(("600001",))[0]
    assert len(first.risk_events) == 1 and first.risk_events[0].risk_level == 4
    assert first.risk_events[0].first_disclosed_at == old
    assert first.announcement_state is CompanyDataState.PARTIAL
    assert any(item.reason_code == "initial_risk_state_unverified" for item in first.review_reasons)
    assert reader.document_links("600001", ("old-case",))
    index.publish(("600001",), _AT + timedelta(minutes=1))
    assert reader.read(("600001",))[0].risk_events == first.risk_events
    _coverage(index, listed)  # Fixture evidence of a completed historical audit, not a live supplier check.
    index.publish(("600001",), _AT + timedelta(minutes=2))
    complete = reader.read(("600001",))[0]
    assert complete.announcement_state is CompanyDataState.COMPLETE
    assert complete.risk_events == first.risk_events
    assert not any(item.reason_code == "initial_risk_state_unverified" for item in complete.review_reasons)


@pytest.mark.parametrize(
    "body",
    (None, "本公司收到证监会立案告知书〔2025〕调查1号。另案〔2025〕调查2号本公司未被立案。"),
)
def test_old_pending_or_ambiguous_document_survives_pruning_and_blocks_complete_coverage(tmp_path, body):
    listed = _AT.date() - timedelta(days=1000)
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=(CompanyListing("600001", listed),),
    )
    index.initialize()
    _old_document(index, body)
    _coverage(index, listed)
    reader = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path)
    for minute in (0, 1):
        index.publish(("600001",), _AT + timedelta(minutes=minute))
        facts = reader.read(("600001",))[0]
        assert facts.announcement_state is CompanyDataState.PARTIAL
        assert any(item.evidence_id == "old-case" for item in facts.review_reasons)
        assert reader.document_links("600001", ("old-case",))


@pytest.mark.parametrize("known_listing", (False, True))
def test_missing_listing_is_unknown_while_coverage_since_recent_listing_is_provable(tmp_path, known_listing):
    listings = (CompanyListing("600001", _AT.date() - timedelta(days=30)),) if known_listing else ()
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=listings,
    )
    index.initialize()
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    facts = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))[0]
    assert (facts.announcement_state is CompanyDataState.COMPLETE) is known_listing
    assert (
        any(item.reason_code == "initial_risk_state_unverified" for item in facts.review_reasons) is not known_listing
    )


def test_old_extraction_identity_cannot_be_silently_loaded_under_new_rules(tmp_path):
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
    )
    index.initialize()
    index.publish(("600001",), _AT)
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE company_publication SET extraction_hash=?", (b"0" * 32,))
    with pytest.raises(ValueError, match="company_publication_identity_mismatch"):
        SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001",))


def test_supplement_combines_history_ocr_limits_and_reuses_completed_data(tmp_path):
    listed = _AT.date() - timedelta(days=410)
    listings = (CompanyListing("600001", listed),)
    path = tmp_path / "collection.sqlite3"
    facts_path = tmp_path / "facts.sqlite3"
    index = SQLiteCompanyEvidenceIndex(path, facts_path=facts_path, documents=CompanyDocumentFiles(tmp_path / "text"), listings=listings)
    index.initialize()
    old = _AT - timedelta(days=400)
    documents = tuple(Disclosure("cninfo", identity, ("600001",), "收到立案告知书", old, "https://example.test/old", identity) for identity in ("old-case", "ocr-case", "limits-case"))
    partition = DisclosurePartition("sse", old.date(), old.date())
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO partitions(market,start_date,end_date) VALUES('sse',?,?)", (str(old.date()), str(old.date())))
    index.commit_page(partition, DisclosurePage(documents, 3, False), _AT)
    index.document_failure(documents[1], "previous-ocr-policy", "disclosure_document_no_text", _AT, _AT + timedelta(days=1))
    index.document_failure(documents[2], "previous-limits-policy", "disclosure_document_page_limit", _AT, _AT + timedelta(days=1))
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    history = SQLiteCompanyEvidenceIndex(path, facts_path=facts_path, documents=CompanyDocumentFiles(tmp_path / "text"), listings=listings, history_start=listed)
    attempts = []

    class Source(fixtures.Sources):
        def fetch(self, query, **kwargs):
            if isinstance(query, DisclosurePartition) and query.start <= (_AT - timedelta(days=400)).date() <= query.end:
                return DisclosurePage(documents, 3, False)
            return super().fetch(query, **kwargs)

        def document(self, item, **kwargs):
            attempts.append((item.identity, kwargs.get("route", "standard")))
            return fixtures._extraction("本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")

    source = Source()
    request = CompanyEvidenceRequest(("600001",), _AT, fixtures._TEST_COMPANY_CONFIGURATION, listings=listings, supplement=True)
    dependencies = CompanyEvidenceDependencies(history, source, source, lambda: 0)
    result = SyncCompanyEvidence(dependencies, request)
    assert not result.needs_retry and result.window_days == 410
    assert set(attempts) == {("old-case", "standard"), ("ocr-case", "ocr"), ("limits-case", "limits")}
    assert ProjectSyncResult(SyncResult((SyncTaskResult("company", "completed", company=result),)))["tasks"][0]["window_days"] == 410
    facts = SQLiteCompanyEvidenceReader(facts_path, raw_path=path).read(("600001",))[0]
    assert facts.announcement_state is CompanyDataState.COMPLETE and len(facts.risk_events) == 1
    assert not any(review.reason_code == "initial_risk_state_unverified" for review in facts.review_reasons)
    source.calls.clear()
    again = SyncCompanyEvidence(dependencies, request)
    assert not again.needs_retry and not source.calls and len(attempts) == 3


def test_ordinary_finish_keeps_unfinished_old_history_cursor(tmp_path):
    listed = _AT.date() - timedelta(days=410)
    index = SQLiteCompanyEvidenceIndex(tmp_path / "collection.sqlite3", facts_path=tmp_path / "facts.sqlite3", documents=CompanyDocumentFiles(tmp_path / "text"), listings=(CompanyListing("600001", listed),))
    index.initialize()
    day = _AT.date() - timedelta(days=405)
    partition = DisclosurePartition("sse", day, day)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO partitions(market,start_date,end_date) VALUES('sse',?,?)", (str(day), str(day)))
    item = fixtures._disclosure("old-page", when=_AT - timedelta(days=405))
    index.commit_page(partition, DisclosurePage((item,), 2, True), _AT)
    index.finish(_AT)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT next_page FROM partitions WHERE market='sse' AND start_date=?", (str(day),)).require_one()[0] == 2


def test_extraction_upgrade_rebuilds_case_status_and_does_not_stamp_unprocessed_rows(tmp_path):
    listed = _AT.date() - timedelta(days=1000)
    index = SQLiteCompanyEvidenceIndex(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=(CompanyListing("600001", listed),),
    )
    index.initialize()
    _old_document(index, "本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")
    _coverage(index, listed)
    index.publish(("600001", "600002"), _AT)
    # Simulate a previous extractor having wrongly closed the still-active case.
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE company_risk_event SET status=2,resolved_at=last_disclosed_at,expires_at=last_disclosed_at WHERE code='600001'",
        )
        connection.run("UPDATE company_publication SET extraction_hash=?", (b"0" * 32,))
    index.prepare_publication(("600001", "600002"), _AT)
    _old_document(index, "本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")
    index.publish(("600001",), _AT + timedelta(minutes=1))
    facts = SQLiteCompanyEvidenceReader(index.facts_path, raw_path=index.path).read(("600001", "600002"))
    assert tuple(item.code for item in facts) == ("600001", "600002")
    assert facts[1].published_at == _AT + timedelta(minutes=1)
    assert facts[0].risk_events[0].status.value == "active"
    assert facts[0].risk_events[0].resolved_at is None
