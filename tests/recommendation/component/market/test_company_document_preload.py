"""Real formal links enter one publication cache before the HTTP request starts."""

import hashlib
import time
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.helpers.download.company.company_startup import _AT, _DATES, _snapshot
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture
from tests.helpers.recommendation.publication.freeze_queries import _Clock, _FreezeRecords
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_events import CompanyEvidenceLink
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.domain.company_source_records import CompanyFinancialSourceObservation, CompanyFinancialSourceRecordHash
from trader.download.infra.company_facts_index import CompanyInputReceipt
from trader.download.infra.company_financial_observations import (
    COMPANY_FINANCIAL_SCORING_POLICY_PREFIX, CompanyFinancialScoringSourceKey,
)
from trader.http_api.route_services import UnifiedWebServices
from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedFreezeEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import FreezeQuerySources, UnifiedFreezeQueries
from trader.recommendation.application.pipeline.freeze_publish.draft_index import UnifiedFreezeDraftIndex
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.web import CreateApp


def _publication(tmp_path, *, url="https://example.test/investigation.pdf"):
    snapshot = _snapshot()
    document, = snapshot.announcements
    digest = hashlib.sha256(document.text.encode()).hexdigest()
    disclosure = replace(document.disclosure, digest=digest, url=url)
    snapshot = replace(snapshot, announcements=(replace(document, disclosure=disclosure),))
    facts = CompileCompanyFacts(snapshot, CompanyEventPolicyFixture())
    writer = SQLiteCompanyFactsIndexFixture(tmp_path / "company.sqlite3")
    writer.initialize()
    link = CompanyPublishedEvidenceLink(
        facts.code, disclosure.source, digest,
        CompanyEvidenceLink(disclosure.identity, disclosure.title, url, _AT, True),
    )
    receipt = CompanyInputReceipt(
        disclosure.source, disclosure.identity, facts.code, digest, writer.extraction_hash,
        hashlib.sha256(document.text.encode()).digest(), _AT,
    )
    writer.publish((facts,), {disclosure.identity: disclosure.source}, None, _AT,
                   receipts=(receipt,), document_links=(link,))
    reader = SQLiteCompanyEvidenceReaderFixture(writer.path)
    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: _AT, open_dates=lambda: _DATES, monotonic=time.monotonic,
    )
    return writer, reader, research, link


@pytest.mark.parametrize("url", ("https://example.test/investigation.pdf", "javascript:private()"))
def test_real_formal_links_reach_actual_http_usecase_without_request_io(tmp_path, monkeypatch, url):
    writer, reader, research, link = _publication(tmp_path, url=url)
    formal = reader.read_document_links((link.code,))
    assert formal.links == (link,)
    research.initialize((link.code,), cancelled=lambda: False)
    assert research.loaded_identity == formal.publication_hash
    queries = UnifiedFreezeQueries(
        UnifiedFreezeIndex(), UnifiedFreezeDraftIndex(), _FreezeRecords(), _Clock(),
        sources=FreezeQuerySources(company_documents=research),
    )
    client = CreateApp(services=UnifiedWebServices(queries, UnifiedFreezeEventStream(), lambda: {})).test_client()

    def forbidden(*args, **kwargs):
        pytest.fail("company HTTP request attempted external I/O after preload")

    for method in ("visit", "read_document_links", "publication_version", "publication_identity", "read_financial_scoring_observations"):
        monkeypatch.setattr(reader, method, forbidden)
    monkeypatch.setattr("trader.download.infra.company_facts_reader.SqliteSession", forbidden)
    monkeypatch.setattr("socket.socket", forbidden)
    response = client.get(f"/api/company/{link.code}/documents?evidence_id={link.link.evidence_id}")
    assert response.status_code == 200
    assert response.get_json()["documents"] == [{
        "evidence_id": link.link.evidence_id, "title": link.link.title,
        "url": url if url.startswith("https://") else None,
        "published_at": _AT.isoformat(), "body_checked": True,
    }]
    assert client.get(f"/api/company/{link.code}/documents?evidence_id=unknown").get_json()["documents"] == []
    assert client.get(f"/api/company/{link.code}/documents").status_code == 400
    assert writer.path.exists()


def test_mismatched_link_publication_is_never_adopted_with_old_facts(tmp_path, monkeypatch):
    _, reader, research, link = _publication(tmp_path)
    read = reader.read_document_links
    calls = []

    def mismatched(codes, *, cancel_requested=None):
        calls.append(tuple(codes))
        return replace(read(codes, cancel_requested=cancel_requested), publication_hash="f" * 64)

    monkeypatch.setattr(reader, "read_document_links", mismatched)
    research.initialize((link.code,), cancelled=lambda: False)
    assert len(calls) >= 1
    assert research.loaded_identity == "company:unavailable"
    assert research.status().last_error == "company_publication_changed"
    assert research.document_links(link.code, (link.link.evidence_id,)) == ()


def test_link_cache_is_included_in_preload_reservation(tmp_path):
    _, reader, _, link = _publication(tmp_path)
    reservations = []

    def reserve(size):
        reservations.append(size)
        return False

    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: _AT, open_dates=lambda: _DATES, monotonic=time.monotonic, reserve_bytes=reserve,
    )
    research.initialize((link.code,), cancelled=lambda: False)
    assert len(reservations) == 1 and reservations[0] > 8192 + 1024
    assert research.status().last_error == "company_preload_capacity"
    assert research.document_links(link.code, (link.link.evidence_id,)) == ()


def test_scoring_receipt_is_only_available_after_sealed_adoption(tmp_path):
    snapshot = _snapshot()
    financial = snapshot.financials[0]
    source_identity = f"{snapshot.code}.SH"
    supplier = CompanyFinancialSourceObservation(
        financial, source_identity, financial.published_at, None, _AT, "CNY",
        CompanyFinancialSourceRecordHash(financial, source_identity, financial.published_at, None, "CNY"), (),
    )
    receipt_at = _AT + timedelta(seconds=1)
    adopted_at = receipt_at + timedelta(seconds=1)
    scope = "a" * 64
    facts = CompileCompanyFacts(replace(snapshot, financials=(), announcements=()), CompanyEventPolicyFixture())
    writer = SQLiteCompanyFactsIndexFixture(tmp_path / "company.sqlite3")
    writer.initialize()
    receipt = CompanyInputReceipt(
        "eastmoney_financial", CompanyFinancialScoringSourceKey(scope, supplier), snapshot.code, supplier.record_hash,
        writer.extraction_hash, None, receipt_at, policy_id=COMPANY_FINANCIAL_SCORING_POLICY_PREFIX + scope,
    )
    publication = writer.publish((facts,), {}, None, _AT, receipts=(receipt,), financial_observations=(supplier,))
    reader = SQLiteCompanyEvidenceReaderFixture(writer.path)
    assert reader.read_financial_scoring_observations((snapshot.code,), as_of=_AT) == ()
    startup_times = iter((receipt_at, adopted_at))
    research = PublishedCompanyResearchFixture(
        reader, wall_clock=lambda: next(startup_times, adopted_at), open_dates=lambda: _DATES, monotonic=time.monotonic,
        financial_scoring_reader=reader,
    )
    research.initialize((snapshot.code,), cancelled=lambda: False)
    assert research.loaded_identity == publication.identity.content_hash.hex()
    before = research.cached((snapshot.code,), include_structured=True, as_of=receipt_at)[snapshot.code]
    current = research.cached((snapshot.code,), include_structured=True)[snapshot.code]
    assert before.financial_scoring_observation is None
    assert current.financial_scoring_observation is not None
    assert current.financial_scoring_observation.observation == supplier
    assert current.financial_scoring_observation.available_at == receipt_at
    assert current.financial_scoring_adopted_at == adopted_at
    assert current.loaded_at == receipt_at
    assert current.financial is None and not current.financial_history_complete
    assert current.financial_history == ()
