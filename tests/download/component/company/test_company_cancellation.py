"""Cancellation interrupts company I/O without discarding durable pages or publication."""

from threading import Event

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_sources import Session
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture as CompanyDocumentFiles, CompanyResearchFixture
from tests.helpers.download.company_resources import EastmoneyCompanySourceFixture as EastmoneyCompanySource
from trader.download.domain.company_sync import CompanySourceCancelled




def test_cancellation_before_initialization_does_not_create_database(tmp_path):
    index = CompanyResearchFixture(
        tmp_path / "evidence.sqlite3",
        facts_path=(tmp_path / "evidence.sqlite3").parent / "company_facts.sqlite3",
        documents=CompanyDocumentFiles((tmp_path / "evidence.sqlite3").parent / "text"),
        cancel_requested=lambda: True,
    )
    with pytest.raises(InterruptedError, match="company_sync_cancelled"):
        index.initialize()
    assert not index.path.exists()
















@pytest.mark.parametrize("during_request", (False, True))
def test_structured_request_receives_cancellation_and_discards_late_response(during_request):
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
    with pytest.raises(CompanySourceCancelled):
        source.pledge_day(fixtures.NOW)
    assert len(session.requests) == int(during_request)
