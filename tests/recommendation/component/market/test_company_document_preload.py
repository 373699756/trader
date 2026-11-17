"""Real formal links enter one publication cache before the HTTP request starts."""

import hashlib
import time
from dataclasses import replace

import pytest

from tests.helpers.download.company.company_startup import _AT, _DATES, _snapshot
from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from tests.helpers.download.configuration import CompanyEventPolicyFixture
from tests.helpers.recommendation.company_research import PublishedCompanyResearchFixture
from tests.helpers.recommendation.publication.freeze_queries import _Clock, _FreezeRecords
from trader.download.domain.company_document_links import CompanyPublishedEvidenceLink
from trader.download.domain.company_events import CompanyEvidenceLink
from trader.download.domain.company_facts import CompileCompanyFacts
from trader.download.infra.company_facts_index import CompanyInputReceipt
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

    for method in ("visit", "read_document_links", "publication_version", "publication_identity"):
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
