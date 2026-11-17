"""Only applicable current report directories suppress a bounded source lookup."""

from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.download.company import company_evidence as fixtures
from tests.helpers.download.company.company_annual_income import _contract, _PERIOD, _REPORTED
from trader.download.application.company_evidence import CompanyEvidenceDependencies, _SyncState
from trader.download.application.company_operations import CompanyOperations
from trader.download.application.company_report_inputs import CollectCompanyReportInputs, PlanCompanyReportInputs
from trader.download.domain.company_evidence import CompanyFinancial, DisclosurePage, DisclosurePartition, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.domain.company_financial_report import CompanyFinancialReportRequest
from trader.download.infra.company_annual_income_index import PrioritizeCompanyAnnualReportInputs
from trader.download.infra.company_financial_report_index import PrioritizeCompanyFinancialReportInputs
from trader.infra.sqlite_connection import SqliteSession


def _quarterly(index):
    reported = fixtures.NOW - timedelta(days=2)
    report = CompanyFinancial("600001", date(2026, 9, 30), reported, None, 5, 1, 3, 4, 5, 100, 90)
    index.commit_structured(
        StructuredCompanyQuery("financial", reported.date(), fixtures.NOW.date()),
        StructuredCompanyPage("financial", 1, 1, 1, financials=(report,)), fixtures.NOW,
    )
    index.publish((report.code,), fixtures.NOW)
    return CompanyFinancialReportRequest(report.code, report.report_date, reported)


def _directory(index, item):
    index.commit_page(
        DisclosurePartition("sse", item.published_at.date(), item.published_at.date()),
        DisclosurePage((item,), 1, False), fixtures.NOW,
    )


@pytest.mark.parametrize("kind", ("old_version", "multiple_codes", "news"))
def test_unrelated_annual_directory_does_not_suppress_the_current_missing_report(tmp_path, kind):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    item = fixtures._disclosure("existing-annual", "2025年年度报告", when=_REPORTED)
    if kind == "old_version":
        item = replace(item, published_at=_REPORTED - timedelta(days=7))
    elif kind == "multiple_codes":
        item = replace(item, codes=("600001", "600002"))
    else:
        item = replace(item, source="news")
    _directory(index, item)
    assert index.report_input_plan(contract.codes, fixtures.NOW).financial_reports == (
        CompanyFinancialReportRequest("600001", _PERIOD, _REPORTED),
    )


@pytest.mark.parametrize("offset", (-1, 0, 1, 7))
def test_current_or_newer_official_annual_directory_is_reused_without_network(tmp_path, offset):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    item = fixtures._disclosure("current-annual", "2025年年度报告修订版", when=_REPORTED + timedelta(days=offset))
    _directory(index, item)
    assert index.report_input_plan(contract.codes, fixtures.NOW).financial_reports == ()


@pytest.mark.parametrize("kind", ("news", "multiple_codes"))
def test_ineligible_newer_annual_directory_cannot_hide_the_applicable_local_report(tmp_path, kind):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    valid = fixtures._disclosure("applicable-annual", "2025年年度报告", when=_REPORTED)
    invalid = replace(valid, identity="ineligible-newer", digest="other-version", published_at=_REPORTED + timedelta(days=1))
    invalid = replace(invalid, source="news") if kind == "news" else replace(invalid, codes=("600001", "600002"))
    _directory(index, valid)
    _directory(index, invalid)
    assert index.report_input_plan(contract.codes, fixtures.NOW).financial_reports == ()
    assert _annual_priority(index, contract.codes) == (valid.identity,)


@pytest.mark.parametrize("kind", ("old_version", "outside_window", "future"))
def test_ineligible_annual_material_is_not_prioritized_as_a_current_dependency(tmp_path, kind):
    index = fixtures._index(tmp_path)
    contract = _contract(index)
    when = _REPORTED - timedelta(days=7) if kind == "old_version" else (
        fixtures.NOW - timedelta(days=366) if kind == "outside_window" else fixtures.NOW + timedelta(seconds=1)
    )
    item = fixtures._disclosure("ineligible-annual", "2025年年度报告", when=when)
    _directory(index, item)
    assert _annual_priority(index, contract.codes) == ()


def test_known_local_annual_dependency_is_kept_without_inventing_a_source_request(tmp_path):
    index = fixtures._index(tmp_path)
    contract = _contract(index, known_annual=False)
    item = fixtures._disclosure("local-annual", "2025年年度报告", when=_REPORTED)
    _directory(index, item)
    assert index.report_input_plan(contract.codes, fixtures.NOW).financial_reports == ()
    assert _annual_priority(index, contract.codes) == (item.identity,)


def _annual_priority(index, codes):
    with SqliteSession(index.path) as collected, SqliteSession(index.facts_path) as formal:
        collected.run("CREATE TEMP TABLE pending_documents(id INTEGER PRIMARY KEY)")
        collected.run("INSERT INTO pending_documents SELECT id FROM disclosures")
        collected.run("CREATE TEMP TABLE company_field_inputs(id INTEGER PRIMARY KEY)")
        PrioritizeCompanyAnnualReportInputs(collected, formal, codes, fixtures.NOW)
        return tuple(row[0] for row in collected.run(
            "SELECT d.source_key FROM disclosures d JOIN company_field_inputs p ON p.id=d.id ORDER BY d.source_key",
        ))


@pytest.mark.parametrize("kind", ("future", "multiple_codes", "old_version", "news"))
def test_unusable_quarterly_directory_does_not_suppress_the_current_missing_report(tmp_path, kind):
    index = fixtures._index(tmp_path)
    request = _quarterly(index)
    item = fixtures._disclosure("existing-quarter", "2026年第三季度报告", when=request.disclosed_at)
    if kind == "future":
        item = replace(item, published_at=fixtures.NOW + timedelta(seconds=1))
    elif kind == "multiple_codes":
        item = replace(item, codes=("600001", "600002"))
    elif kind == "old_version":
        item = replace(item, published_at=request.disclosed_at - timedelta(days=2))
    else:
        item = replace(item, source="news")
    _directory(index, item)
    assert index.report_input_plan((request.code,), fixtures.NOW).financial_reports == (request,)


def test_accepted_previous_day_quarterly_report_is_reused_and_prioritized_after_restart(tmp_path):
    index = fixtures._index(tmp_path)
    request = _quarterly(index)
    item = fixtures._disclosure("accepted-quarter", "2026年第三季度报告", when=request.disclosed_at - timedelta(days=1))
    assert index.commit_financial_reports(request, (item,), fixtures.NOW) == 1
    restarted = fixtures._index(tmp_path)
    assert restarted.report_input_plan((request.code,), fixtures.NOW).financial_reports == ()
    with SqliteSession(index.path) as collected:
        collected.run("CREATE TEMP TABLE pending_documents(id INTEGER PRIMARY KEY)")
        collected.run("INSERT INTO pending_documents SELECT id FROM disclosures WHERE source_key=?", (item.identity,))
        collected.run("CREATE TEMP TABLE company_field_inputs(id INTEGER PRIMARY KEY)")
        PrioritizeCompanyFinancialReportInputs(collected, (request.code,), fixtures.NOW)
        assert collected.run("SELECT COUNT(*) FROM company_field_inputs").require_one()[0] == 1


@pytest.mark.parametrize("failed", (False, True))
def test_empty_or_failed_report_result_is_not_requested_again_at_an_unchanged_checkpoint(tmp_path, failed):
    index = fixtures._index(tmp_path)
    request = _quarterly(index)

    class Source(fixtures.Sources):
        reports = ()

        def financial_reports(self, request, *, observed_at):
            self.reports += (request,)
            if failed:
                raise TimeoutError("fixture_report_timeout")
            return ()

    source = Source()
    dependencies = CompanyEvidenceDependencies(index, source, source, lambda: 0)
    state = _SyncState(CompanyOperations(None, lambda: 0, ()), 1)
    for _ in range(2):
        plan = PlanCompanyReportInputs(dependencies, (request.code,), fixtures.NOW, state)
        assert CollectCompanyReportInputs(dependencies, plan, state) == 0
    assert source.reports == (request,)
