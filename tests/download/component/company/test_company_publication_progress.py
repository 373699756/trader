"""Publication feedback follows the real formal transaction, including failure paths."""

import pytest

from trader.download.domain.company_evidence import CompanyPublicationProgress, CompanyStockProgress
from tests.helpers.download.company.company_pipeline import NOW, _seed
from tests.helpers.download.company_resources import CompanyDocumentFilesFixture, SQLiteCompanyEvidenceIndexFixture


def test_formal_receipt_follows_verification_and_visible_commit(tmp_path):
    events = []

    def record(event):
        if isinstance(event, CompanyPublicationProgress) and event.state == "committed":
            assert index.published_count(event.codes) == len(event.codes)
        events.append(event)

    index = SQLiteCompanyEvidenceIndexFixture(
        tmp_path / "collection.sqlite3", facts_path=tmp_path / "facts.sqlite3",
        documents=CompanyDocumentFilesFixture(tmp_path / "text"), stock_progress=record,
    )
    index.initialize()
    _seed(index, 1)
    assert index.publish(("600001",), NOW) == 1
    verified, started, committed = events
    assert isinstance(verified, CompanyStockProgress) and not verified.published
    assert started == CompanyPublicationProgress("started", ("600001",))
    assert committed == CompanyPublicationProgress("committed", ("600001",), ("600001",))
    events.clear()
    assert index.publish(("600001",), NOW, changed_only=True) == 0
    assert events == []


def test_failed_formal_transaction_never_emits_a_committed_receipt(tmp_path, monkeypatch):
    events = []
    index = SQLiteCompanyEvidenceIndexFixture(
        tmp_path / "collection.sqlite3", facts_path=tmp_path / "facts.sqlite3",
        documents=CompanyDocumentFilesFixture(tmp_path / "text"), stock_progress=events.append,
    )
    index.initialize()
    _seed(index, 1)

    def fail(*args, **kwargs):
        raise OSError("formal write failed")

    monkeypatch.setattr(index._facts, "publish", fail)
    with pytest.raises(OSError, match="formal write failed"):
        index.publish(("600001",), NOW)
    assert any(isinstance(event, CompanyStockProgress) for event in events)
    assert any(isinstance(event, CompanyPublicationProgress) and event.state == "started" for event in events)
    assert not any(isinstance(event, CompanyPublicationProgress) and event.state == "committed" for event in events)
    assert index.published_count(("600001",)) == 0
