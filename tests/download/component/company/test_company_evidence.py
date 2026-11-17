

import sqlite3

import time


from datetime import timedelta



import pytest

from tests.helpers.shared.reference_fixtures import PreparedFixture



from trader.download.domain.company_evidence import StructuredCompanyPage, StructuredCompanyQuery



from trader.infra.sqlite_connection import (
    SqliteConnectionOptions,
    SqliteSession,
    SqliteTransaction,
)

from tests.helpers.download.company_resources import CompanyDocumentFilesFixture as CompanyDocumentFiles, CompanyResearchFixture, SQLiteCompanyEvidenceReaderFixture as SQLiteCompanyEvidenceReader, SQLiteCompanyFactsIndexFixture as SQLiteCompanyFactsIndex
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture as PublishedCompanyResearch

from tests.helpers.download.company.company_evidence import NOW, Sources, _index




class _FinancialSnapshotSource(Sources):
    def __init__(self, financials):
        super().__init__()
        self.financials = financials
        self.financial_failure = False

    def fetch(self, first, *args, **kwargs):
        if isinstance(first, StructuredCompanyQuery) and first.component == "financial":
            self.calls.append(first)
            if self.financial_failure:
                raise TimeoutError("fixture")
            # Model the supplier's NOTICE_DATE filter, including unchanged old notices.
            financials = tuple(
                item for item in self.financials if first.start <= item.published_at.date() <= first.end
            )
            return StructuredCompanyPage("financial", first.page, 1, len(financials), financials=financials)
        return super().fetch(first, *args, **kwargs)














def test_compact_reader_cancels_before_decoding_and_delivers_no_partial_rows(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    observed = []
    with pytest.raises(InterruptedError, match="company_preload_cancelled"):
        SQLiteCompanyEvidenceReader(index.facts_path).visit(
            ("600001",), observed.append, cancel_requested=lambda: True
        )
    assert observed == []




















def test_reader_does_not_initialize_or_write_missing_database(tmp_path):
    reader = SQLiteCompanyEvidenceReader(tmp_path / "missing.sqlite3")
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert not research.load(("600001",), NOW, include_structured=True)
    assert not reader.path.exists()




def test_reader_releases_transaction_before_consumers_and_pins_all_requested_rows(tmp_path):
    index = _index(tmp_path)
    codes = ("600002", "600001")
    index.publish(codes, NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    before = reader.read(codes)
    observed = []

    def consume(snapshot):
        if not observed:
            index.publish(codes, NOW + timedelta(days=1))
            with (
                SqliteSession(index.path, SqliteConnectionOptions(timeout_seconds=0)) as connection,
                SqliteTransaction(connection),
            ):
                assert connection.run("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
        observed.append(snapshot)

    reader.visit(codes, consume)
    assert tuple(observed) == before
    assert reader.read(codes) != before




def test_fixed_company_inputs_leave_missing_codes_uncovered_after_later_publication(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    research = PublishedCompanyResearch(
        SQLiteCompanyEvidenceReader(index.facts_path),
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert not research.load(("600002",), NOW, include_structured=True)
    index.publish(("600002",), NOW)
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert research.status().entries == 1
    assert not research.status().restart_required
    assert not research.cached(("600002",), include_structured=True)


def test_company_reads_do_not_revisit_disk_after_preload(tmp_path, monkeypatch):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    research.load(("600001",), NOW, include_structured=True)
    before = research.status()

    def unavailable():
        pytest.fail("loaded company facts must not reread publication identity")

    monkeypatch.setattr(reader, "publication_version", unavailable)
    monkeypatch.setattr(reader, "visit", lambda *args: pytest.fail("loaded company facts must not reread content"))
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    assert research.load(("600001",), NOW, include_structured=True)
    assert research.status() == before




def test_old_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "company_evidence.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE risk_evidence_recent(payload TEXT)")
    before = path.read_bytes()
    SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3").initialize()
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        CompanyResearchFixture(
            path,
            facts_path=(path).parent / "company_facts.sqlite3",
            documents=CompanyDocumentFiles((path).parent / "text"),
        ).initialize()
    assert path.read_bytes() == before


def test_unversioned_collection_rejects_writer_and_formal_links_never_inspect_collection(tmp_path):
    path = tmp_path / "company_collection.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection):
        connection.run("CREATE TABLE contract(identity TEXT PRIMARY KEY)")
        connection.run("CREATE TABLE disclosures(source_id TEXT, digest TEXT)")
        connection.run("INSERT INTO disclosures VALUES('first','a')")
    before = path.read_bytes()
    facts_path = tmp_path / "company_facts.sqlite3"
    SQLiteCompanyFactsIndex(facts_path).initialize()
    formal_before = facts_path.read_bytes()
    with pytest.raises(RuntimeError, match="persistence_unavailable"):
        CompanyResearchFixture(
            path, facts_path=facts_path, documents=CompanyDocumentFiles(tmp_path / "text")
        ).initialize()
    snapshot = SQLiteCompanyEvidenceReader(facts_path).read_document_links(("600001",))
    assert snapshot.publication_hash == "" and snapshot.links == ()
    assert path.read_bytes() == before
    assert facts_path.read_bytes() == formal_before


def test_previous_formal_version_is_rejected_without_reinterpreting_facts(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    with SqliteSession(path, SqliteConnectionOptions(journal_mode="WAL")) as connection:
        connection.run("PRAGMA user_version=1")
        connection.run("CREATE TABLE company_facts(code TEXT PRIMARY KEY,financial_report_date INTEGER)")
        connection.run("INSERT INTO company_facts VALUES('600001',20741)")
    before = path.read_bytes()
    reader = SQLiteCompanyEvidenceReader(path)
    with pytest.raises(RuntimeError, match="snapshot_unreadable") as error:
        reader.read(("600001",))
    assert str(error.value.__cause__) == "sqlite_schema_unsupported"
    with pytest.raises(sqlite3.DatabaseError, match="sqlite_schema_unsupported"):
        SQLiteCompanyFactsIndex(path).initialize()
    assert path.read_bytes() == before


def test_corrupt_database_initialization_reports_the_business_error(tmp_path):
    path = tmp_path / "company_evidence.sqlite3"
    path.write_bytes(b"not a SQLite database")
    SQLiteCompanyFactsIndex(tmp_path / "company_facts.sqlite3").initialize()
    with pytest.raises(RuntimeError, match="company_evidence_persistence_unavailable") as error:
        CompanyResearchFixture(
            path,
            facts_path=(path).parent / "company_facts.sqlite3",
            documents=CompanyDocumentFiles((path).parent / "text"),
        ).initialize()
    assert isinstance(error.value.__cause__, sqlite3.DatabaseError)
















def test_corrupt_read_model_is_rejected_and_previous_memory_is_retained(tmp_path):
    index = _index(tmp_path)
    index.publish(("600001",), NOW)
    reader = SQLiteCompanyEvidenceReader(index.facts_path)
    research = PublishedCompanyResearch(
        reader, wall_clock=lambda: NOW, open_dates=lambda: PreparedFixture(NOW).open_dates, monotonic=time.monotonic
    )
    research.initialize(("600001", "600002"), cancelled=lambda: False)
    before = research.load(("600001",), NOW, include_structured=True)
    with SqliteSession(index.facts_path) as connection, SqliteTransaction(connection):
        connection.run("PRAGMA ignore_check_constraints=ON")
        connection.run("UPDATE company_facts SET announcement_state=999")
    with pytest.raises(RuntimeError, match="sqlite_publication_checksum_mismatch"):
        reader.read(("600001",))
    assert research.load(("600001",), NOW, include_structured=True) == before
    restarted = PublishedCompanyResearch(
        reader,
        wall_clock=lambda: NOW,
        open_dates=lambda: PreparedFixture(NOW).open_dates,
        monotonic=time.monotonic,
    )
    restarted.initialize(("600001",), cancelled=lambda: False)
    assert not restarted.load(("600001",), NOW, include_structured=True)
    assert "SqlitePublicationError" in restarted.status().last_error
