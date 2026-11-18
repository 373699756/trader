"""Whole-window checks reject partial or drifting pagination and preserve raw counts."""

import json
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.core.contracts import DiagnosticResult
from scripts.download.company.cli import BuildParser, ParseOptions
from scripts.download.company.commands import BuildCommands
from scripts.download.company.company_structured_window_audit import AuditCompanyStructuredWindow, BuildCompanyStructuredWindowQuery
from scripts.download.company import company_structured_window_audit as window
from scripts.download.company.reporting import NormalizeResult, ProjectDetails
from trader.download.domain.company_evidence import CompanyFinancial, StructuredCompanyQuery
from trader.download.domain.company_source_records import CompanyFinancialMissingField, CompanyFinancialSourceObservation, CompanyFinancialSourcePage, CompanyFinancialSourceRecordHash
from trader.infra.http_contracts import HttpConnectionError, HttpResponse

_NOW = datetime(2026, 11, 18, 12, tzinfo=ZoneInfo("Asia/Shanghai"))
_QUERY = StructuredCompanyQuery("pledge", date(2026, 11, 13), date(2026, 11, 13))


class _Session:
    def __init__(self, *, second_pages=2, duplicate=False, short=False, failure=False):
        self.requests = []
        self.second_pages, self.duplicate, self.short, self.failure = second_pages, duplicate, short, failure

    def get(self, url, **kwargs):
        self.requests.append(kwargs)
        number = int(kwargs["params"]["pageNumber"])
        if self.failure:
            raise HttpConnectionError("private-provider-error")
        start, end = (0, 499 if self.short else 500) if number == 1 else (0 if self.duplicate else 500, 1 if self.duplicate else 501)
        rows = [{"SECURITY_CODE": str(600000 + index), "TRADE_DATE": "2026-11-13 00:00:00", "PLEDGE_RATIO": 0.23}
                for index in range(start, end)]
        return HttpResponse(200, json.dumps({"code": 0, "success": True, "result": {
            "pages": 2 if number == 1 else self.second_pages, "count": 501, "data": rows,
        }}).encode())


def _run(session, *, clock=lambda: 0):
    return AuditCompanyStructuredWindow(session, _QUERY, observed_at=_NOW, timeout_seconds=8, deadline=60,
                                        http_attempts=[], monotonic=clock)


def test_all_pages_must_reconcile_before_pagination_is_verified():
    session = _Session()
    report = _run(session)
    assert report["status"] == "passed" and report["pagination_verified"] is True
    assert report["raw_rows"] == report["decoded_rows"] == report["reported_total"] == 501
    assert report["fetch_invocations"] == report["received_pages"] == 2
    assert [item["params"]["pageNumber"] for item in session.requests] == ["1", "2"]
    assert report["production_adopted"] is False and report["population_coverage_verified"] is False
    assert all(item["timeout"] <= 8 and item["deadline"] <= 60 for item in session.requests)


@pytest.mark.parametrize(("kwargs", "reason"), (
    ({"second_pages": 3}, "company_structured_window_pagination_changed"),
    ({"duplicate": True}, "company_structured_window_duplicate_key"),
    ({"short": True}, "company_structured_window_count_mismatch"),
    ({"failure": True}, "http_transport_error"),
))
def test_partial_changed_or_duplicate_windows_do_not_get_coverage(kwargs, reason):
    report = _run(_Session(**kwargs))
    assert report["status"] == "failed" and report["failure"] == reason
    assert report["pagination_verified"] is False
    assert "private-provider-error" not in json.dumps(report)


def test_exhausted_deadline_does_not_start_a_request():
    session = _Session()
    assert _run(session, clock=lambda: 60)["status"] == "timed_out"
    assert session.requests == []


def test_rejected_financial_page_does_not_inflate_accepted_null_or_hash_counts(monkeypatch):
    def observed(code):
        report = CompanyFinancial(code, date(2026, 9, 30), _NOW, None, 1, 1, 1, 1, 1, 1, 1)
        identity = f"{code}.SH"
        return CompanyFinancialSourceObservation(report, identity, _NOW, _NOW, _NOW, "CNY",
            CompanyFinancialSourceRecordHash(report, identity, _NOW, _NOW, "CNY"), (CompanyFinancialMissingField("basic_eps"),))

    pages = [CompanyFinancialSourcePage(1, 2, 501, 500, (observed("600001"),), "a" * 64),
             CompanyFinancialSourcePage(2, 3, 501, 1, (observed("600002"),), "b" * 64)]

    class Source:
        def __init__(self, *args, **kwargs):
            pass

        def financial_observations(self, query, *, observed_at):
            return pages.pop(0)

    monkeypatch.setattr(window, "EastmoneyCompanySource", Source)
    report = AuditCompanyStructuredWindow(object(), StructuredCompanyQuery("financial", _NOW.date(), _NOW.date()),
        observed_at=_NOW, timeout_seconds=8, deadline=60, http_attempts=[], monotonic=lambda: 0)
    assert report["status"] == "failed" and report["failure"] == "company_structured_window_pagination_changed"
    assert report["decoded_rows"] == report["source_null_fields"] == 1 and report["response_hashes"] == ["a" * 64]


def test_window_uses_one_year_and_known_pledge_day_and_future_unlock_bounds():
    financial = BuildCompanyStructuredWindowQuery("financial", date(2025, 11, 18), _NOW.date(), observed_at=_NOW)
    assert financial.start == date(2025, 11, 18)
    for family, start, end in (("financial", date(2025, 11, 17), _NOW.date()),
                               ("pledge", date(2026, 11, 12), date(2026, 11, 13)),
                               ("unlock", _NOW.date(), date(2027, 5, 18))):
        with pytest.raises(ValueError):
            BuildCompanyStructuredWindowQuery(family, start, end, observed_at=_NOW)


def test_public_source_command_forwards_window_without_stock_filter_or_adoption():
    options = ParseOptions(BuildParser("research-source-compare").parse_args([
        "--structured-window", "financial", "--structured-start", "2026-08-01", "--structured-end", "2026-08-31",
    ]))
    command, = BuildCommands(options)
    assert command.argv[-6:] == ("--structured-window", "financial", "--structured-start", "2026-08-01", "--structured-end", "2026-08-31")
    report = _run(_Session())
    report["private"] = "private-payload"
    report["fields"] = [{"field": "basic_eps", "source_null_rows": 1, "private": "private-payload"}]
    result = NormalizeResult(DiagnosticResult("research_source_compare", 0, 1, report, None))
    assert result.error_code is None
    projected = {}
    ProjectDetails(result, report, projected)
    assert "private-payload" not in json.dumps(projected)
    report["population_coverage_verified"] = True
    assert NormalizeResult(DiagnosticResult("research_source_compare", 0, 1, report, None)).error_code is not None
