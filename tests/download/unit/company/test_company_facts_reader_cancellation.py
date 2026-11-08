"""Published company reads respect cancellation before SQL, during SHA and before delivery."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.company_resources import SQLiteCompanyEvidenceReaderFixture, SQLiteCompanyFactsIndexFixture
from trader.download.domain.company_events import CompanyDataState
from trader.download.domain.company_facts import CompanyFacts
from trader.download.infra import company_facts_reader
from trader.infra import sqlite_publication


_AT = datetime(2026, 11, 6, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def reader(tmp_path):
    path = tmp_path / "company_facts.sqlite3"
    writer = SQLiteCompanyFactsIndexFixture(path)
    writer.initialize()
    fact = CompanyFacts(
        code="600001", financial=None, financial_state=CompanyDataState.UNAVAILABLE,
        pledge=None, pledge_state=CompanyDataState.UNAVAILABLE, unlock_schedule=(), unlock_checked_at=None,
        unlock_coverage_end=None, unlock_state=CompanyDataState.UNAVAILABLE, risk_events=(), positive_events=(),
        review_reasons=(), pending_document_count=0, announcement_coverage_start=None,
        announcement_coverage_end=None, announcement_state=CompanyDataState.UNAVAILABLE,
        checked_at=_AT, published_at=_AT,
        latest_financial=None,
    )
    writer.publish((fact,), {}, None, _AT)
    return SQLiteCompanyEvidenceReaderFixture(path)


@pytest.mark.parametrize("operation", ("fields", "facts"))
@pytest.mark.parametrize("codes", ((), ("600001",)))
def test_already_cancelled_small_read_fails_before_hash_or_sql_delivery(reader, monkeypatch, operation, codes):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("cancelled reader must not start file verification")

    monkeypatch.setattr(company_facts_reader, "VerifySqliteFile", forbidden)
    consumed = []
    with pytest.raises(InterruptedError, match="company_field_read_cancelled" if operation == "fields" else "company_preload_cancelled"):
        if operation == "fields":
            reader.read_fields(codes, cancel_requested=lambda: True)
        else:
            reader.visit(codes, consumed.append, cancel_requested=lambda: True)
    assert consumed == []


@pytest.mark.parametrize("operation", ("fields", "facts"))
def test_cancellation_reaches_actual_file_digest_and_delivers_no_partial_rows(reader, monkeypatch, operation):
    original = sqlite_publication._digest
    cancelled = False

    def cancel_at_digest(path, size, check):
        nonlocal cancelled
        assert check is not None, "company reader must pass cancellation into the checksum owner"
        cancelled = True
        return original(path, size, check)

    monkeypatch.setattr(sqlite_publication, "_digest", cancel_at_digest)
    consumed = []
    with pytest.raises(InterruptedError, match="company_field_read_cancelled" if operation == "fields" else "company_preload_cancelled"):
        if operation == "fields":
            reader.read_fields(("600001",), cancel_requested=lambda: cancelled)
        else:
            reader.visit(("600001",), consumed.append, cancel_requested=lambda: cancelled)
    assert cancelled and consumed == []


def test_field_read_rechecks_cancellation_after_a_small_decode(reader, monkeypatch):
    original = company_facts_reader.ReadCompanyFieldResults
    cancelled = False

    def cancel_after_decode(connection, codes):
        nonlocal cancelled
        result = original(connection, codes)
        cancelled = True
        return result

    monkeypatch.setattr(company_facts_reader, "ReadCompanyFieldResults", cancel_after_decode)
    with pytest.raises(InterruptedError, match="company_field_read_cancelled"):
        reader.read_fields((), cancel_requested=lambda: cancelled)


@pytest.mark.parametrize("operation", ("fields", "facts"))
def test_cancelled_missing_publication_does_not_report_a_successful_empty_read(tmp_path, operation):
    reader = SQLiteCompanyEvidenceReaderFixture(tmp_path / "missing.sqlite3")
    with pytest.raises(InterruptedError, match="company_field_read_cancelled" if operation == "fields" else "company_preload_cancelled"):
        if operation == "fields":
            reader.read_fields((), cancel_requested=lambda: True)
        else:
            reader.visit((), lambda _fact: None, cancel_requested=lambda: True)
