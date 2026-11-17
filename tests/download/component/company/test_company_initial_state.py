"""One-year discovery bounds preserve previously verified ongoing cases."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import CompanyListing, Disclosure, DisclosurePage, DisclosurePartition
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from tests.helpers.download.company_resources import CompanyResearchFixture, CompanyDocumentFilesFixture as CompanyDocumentFiles, SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader

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
    document = Disclosure("cninfo", "old-case", ("600001",), "收到立案告知书", old, "https://example.test/old", "a" * 64)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("INSERT INTO partitions(market,start_date,end_date) VALUES('sse',?,?)", (str(old.date()), str(old.date())))
    index.commit_page(partition, DisclosurePage((document,), 1, False), _AT)
    if body is not None:
        index.commit_document(document, fixtures._extraction(body), _AT)
    return old


def test_first_publication_retains_verified_old_case_without_listing_history_gate(tmp_path):
    listed = _AT.date() - timedelta(days=1000)
    index = CompanyResearchFixture(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=(CompanyListing("600001", listed),),
    )
    index.initialize()
    old = _old_document(index, "本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    first = reader.read(("600001",))[0]
    assert len(first.risk_events) == 1 and first.risk_events[0].risk_level == 4
    assert first.risk_events[0].first_disclosed_at == old
    assert first.announcement_state is CompanyDataState.COMPLETE
    assert not any(item.reason_code == "initial_risk_state_unverified" for item in first.review_reasons)
    assert reader.read_document_links(("600001",)).links[0].link.evidence_id == "old-case"
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
def test_expired_unprocessed_document_is_out_of_scope_but_verified_ambiguity_survives(tmp_path, body):
    listed = _AT.date() - timedelta(days=1000)
    index = CompanyResearchFixture(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=(CompanyListing("600001", listed),),
    )
    index.initialize()
    _old_document(index, body)
    _coverage(index, listed)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    for minute in (0, 1):
        index.publish(("600001",), _AT + timedelta(minutes=minute))
        facts = reader.read(("600001",))[0]
        assert (facts.announcement_state is CompanyDataState.PARTIAL) is (body is not None)
        assert any(item.evidence_id == "old-case" for item in facts.review_reasons) is (body is not None)
        links = reader.read_document_links(("600001",)).links
        assert bool(links) is (body is not None)
        if links:
            assert links[0].link.evidence_id == "old-case"


@pytest.mark.parametrize("known_listing", (False, True))
def test_one_year_coverage_does_not_require_history_since_listing(tmp_path, known_listing):
    listings = (CompanyListing("600001", _AT.date() - timedelta(days=30)),) if known_listing else ()
    index = CompanyResearchFixture(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
        listings=listings,
    )
    index.initialize()
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    facts = SQLiteCompanyEvidenceReader(index.facts_path).read(("600001",))[0]
    assert facts.announcement_state is CompanyDataState.COMPLETE
    assert not any(item.reason_code == "initial_risk_state_unverified" for item in facts.review_reasons)


def test_old_extraction_identity_cannot_be_silently_loaded_under_new_rules(tmp_path):
    index = CompanyResearchFixture(
        tmp_path / "facts.sqlite3",
        facts_path=(tmp_path / "facts.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "facts.sqlite3").parent / "text"),
    )
    index.initialize()
    index.publish(("600001",), _AT)
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE company_publication SET extraction_hash=?", (b"0" * 32,))
    index._facts.publish_file()
    with pytest.raises(ValueError, match="company_publication_identity_mismatch"):
        SQLiteCompanyEvidenceReader(index.facts_path).read(("600001",))
