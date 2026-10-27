"""Initial company risk state needs history provenance, including cross-year active cases."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_evidence import CompanyListing, Disclosure, DisclosurePage, DisclosurePartition
from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

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
        connection.run("INSERT INTO partitions(market,start,end) VALUES('sse',?,?)", (str(old.date()), str(old.date())))
    index.commit_page(partition, DisclosurePage((document,), 1, False), _AT)
    if body is not None:
        index.commit_document(document, body)
    return old


def test_first_publication_retains_old_active_case_and_exposes_unverified_initial_state(tmp_path):
    listed = _AT.date() - timedelta(days=1000)
    index = SQLiteCompanyEvidenceIndex(tmp_path / "facts.sqlite3", listings=(CompanyListing("600001", listed),))
    index.initialize()
    old = _old_document(index, "本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    reader = SQLiteCompanyEvidenceReader(index.path)
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
    index = SQLiteCompanyEvidenceIndex(tmp_path / "facts.sqlite3", listings=(CompanyListing("600001", listed),))
    index.initialize()
    _old_document(index, body)
    _coverage(index, listed)
    reader = SQLiteCompanyEvidenceReader(index.path)
    for minute in (0, 1):
        index.publish(("600001",), _AT + timedelta(minutes=minute))
        facts = reader.read(("600001",))[0]
        assert facts.announcement_state is CompanyDataState.PARTIAL
        assert any(item.evidence_id == "old-case" for item in facts.review_reasons)
        assert reader.document_links("600001", ("old-case",))


@pytest.mark.parametrize("known_listing", (False, True))
def test_missing_listing_is_unknown_while_coverage_since_recent_listing_is_provable(tmp_path, known_listing):
    listings = (CompanyListing("600001", _AT.date() - timedelta(days=30)),) if known_listing else ()
    index = SQLiteCompanyEvidenceIndex(tmp_path / "facts.sqlite3", listings=listings)
    index.initialize()
    _coverage(index, _AT.date() - timedelta(days=365))
    index.publish(("600001",), _AT)
    facts = SQLiteCompanyEvidenceReader(index.path).read(("600001",))[0]
    assert (facts.announcement_state is CompanyDataState.COMPLETE) is known_listing
    assert (
        any(item.reason_code == "initial_risk_state_unverified" for item in facts.review_reasons) is not known_listing
    )


def test_old_extraction_identity_cannot_be_silently_loaded_under_new_rules(tmp_path):
    index = SQLiteCompanyEvidenceIndex(tmp_path / "facts.sqlite3")
    index.initialize()
    index.publish(("600001",), _AT)
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run("UPDATE publication SET extraction_identity='official_company_events:previous-rules'")
    with pytest.raises(ValueError, match="company_publication_identity_mismatch"):
        SQLiteCompanyEvidenceReader(index.path).read(("600001",))


def test_extraction_upgrade_rebuilds_case_status_and_does_not_stamp_unprocessed_rows(tmp_path):
    listed = _AT.date() - timedelta(days=1000)
    index = SQLiteCompanyEvidenceIndex(tmp_path / "facts.sqlite3", listings=(CompanyListing("600001", listed),))
    index.initialize()
    _old_document(index, "本公司收到证监会立案告知书〔2025〕调查1号，仍在调查中。")
    _coverage(index, listed)
    index.publish(("600001", "600002"), _AT)
    # Simulate a previous extractor having wrongly closed the still-active case.
    with SqliteSession(index.path) as connection, SqliteTransaction(connection):
        connection.run(
            "UPDATE published SET risk_events_json=json_set(risk_events_json,'$[0].status','resolved',"
            "'$[0].resolved_at',?,'$[0].expires_at',?) WHERE code='600001'",
            (_AT.isoformat(), _AT.isoformat()),
        )
        connection.run("UPDATE publication SET extraction_identity='official_company_events:previous-rules'")
    index.publish(("600001",), _AT + timedelta(minutes=1))
    facts = SQLiteCompanyEvidenceReader(index.path).read(("600001", "600002"))
    assert tuple(item.code for item in facts) == ("600001", "600002")
    assert facts[1].published_at == _AT + timedelta(minutes=1)
    assert facts[0].risk_events[0].status.value == "active"
    assert facts[0].risk_events[0].resolved_at is None
