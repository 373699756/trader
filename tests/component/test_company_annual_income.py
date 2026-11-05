"""Annual revenue travels through the real source decoder, ledger and formal reader."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from threading import Event
from zoneinfo import ZoneInfo

import pytest

from tests.component import test_company_evidence as fixtures
from tests.helpers.configuration import EastmoneyCompanySourceFixture
from trader.download.application.company_evidence import CompanyEvidenceDependencies
from trader.download.domain.company_annual_income import (
    ApplyCompanyAnnualIncome,
    CompanyAnnualIncome,
    CompanyAnnualIncomeRequest,
    SelectCompanyAnnualIncome,
)
from trader.download.domain.company_evidence import CompanyFinancial, DisclosurePage, DisclosurePartition, StructuredCompanyPage, StructuredCompanyQuery
from trader.download.infra.company_fact_rows import ReadCompanyFactRows
from trader.infra.http_contracts import HttpCancelled, HttpResponse, HttpTimeout
from trader.infra.sqlite_connection import SqliteSession

_ZONE = ZoneInfo("Asia/Shanghai")
_REPORTED = datetime(2026, 4, 17, tzinfo=_ZONE)
_SIGNED = datetime(2026, 5, 20, tzinfo=_ZONE)
_PERIOD = date(2025, 12, 31)
# SSE issuer report 600519_20260417_9QS4.pdf, pp. 6 and 61: operating
# revenue differs from total operating income. Only these labelled scalars
# are retained here; the supplier response and the PDF are not fixtures.
_OPERATING_REVENUE = 168_838_102_514.79
_TOTAL_OPERATING_INCOME = 172_054_171_890.91


def _financial(**changes):
    return replace(CompanyFinancial("600001", _PERIOD, _REPORTED, 1, 1, 1, 1, 1, 1, 1, 1), **changes)


def _report():
    return {
        "SECUCODE": "600001.SH", "SECURITY_CODE": "600001", "REPORT_DATE": "2025-12-31 00:00:00",
        "REPORT_TYPE": "年报", "NOTICE_DATE": "2026-04-17 00:00:00", "UPDATE_DATE": "2026-04-17 00:00:00",
        "CURRENCY": "CNY", "OPERATE_INCOME": _OPERATING_REVENUE,
        "TOTAL_OPERATE_INCOME": _TOTAL_OPERATING_INCOME,
    }


class _Session:
    def __init__(self, row=None, *, html=None):
        self.row = _report() if row is None else row
        self.html = '<input id="hidctype" value="4" />' if html is None else html
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        if url.endswith("/Index"):
            return HttpResponse(200, self.html.encode())
        return HttpResponse(200, json.dumps({"data": [self.row]}).encode())


def _income(**changes):
    return replace(CompanyAnnualIncome(
        "600001", _PERIOD, _REPORTED, _REPORTED, fixtures.NOW, "CNY", _OPERATING_REVENUE, "a" * 64
    ), **changes)


def _read(index):
    with SqliteSession(index.facts_path) as connection:
        return ReadCompanyFactRows(connection, ("600001",))[0]


def _contract(index, *, reference="公司2025年度经审计的营业收入", known_annual=True):
    item = replace(
        fixtures._disclosure("contract-income", "关于签订重大合同的公告"), published_at=_SIGNED
    )
    partition = DisclosurePartition("sse", _SIGNED.date(), _SIGNED.date())
    index.commit_page(partition, DisclosurePage((item,), 1, False), fixtures.NOW)
    text = (
        "本公司已签订正式合同，合同已生效。合同金额人民币2亿元（不含税）。"
        f"本合同的金额与{reference}进行比较。"
        "本公告仅说明上述合同的既有事实，公司将按照相关规定持续披露后续进展，相关资料以本次正式披露为准。"
    )
    index.commit_document(item, fixtures._extraction(text), fixtures.NOW)
    if known_annual:
        financial = _financial()
        query = StructuredCompanyQuery("financial", _REPORTED.date(), fixtures.NOW.date())
        index.commit_structured(query, StructuredCompanyPage("financial", 1, 1, 1, financials=(financial,)), fixtures.NOW)
    index.publish(item.codes, fixtures.NOW)
    return item


def test_source_reads_independent_operating_revenue_and_preserves_provenance():
    session = _Session(html="<input value='4' id='hidctype'>")
    source = EastmoneyCompanySourceFixture(session, monotonic=lambda: 0)
    result = source.annual_income(CompanyAnnualIncomeRequest("600001", (_PERIOD,)), observed_at=fixtures.NOW)
    assert len(session.requests) == 2 and len(result) == 1
    income = result[0]
    assert income.operating_revenue == _OPERATING_REVENUE != _TOTAL_OPERATING_INCOME
    assert income.report_date == _PERIOD and income.currency == "CNY"
    assert income.disclosed_at == income.updated_at == _REPORTED and len(income.record_hash) == 64
    assert session.requests[1][1]["params"]["dates"] == "2025-12-31"
    assert session.requests[0][1]["timeout"] == session.requests[1][1]["timeout"] == 8


@pytest.mark.parametrize("kind", ("wrong_code", "quarter", "missing_update", "future", "outside_window", "nonfinite"))
def test_source_rejects_unverified_report_identity_dates_and_values(kind):
    row = _report()
    if kind == "wrong_code":
        row["SECUCODE"] = "600002.SH"
    elif kind == "quarter":
        row["REPORT_TYPE"] = "三季报"
    elif kind == "missing_update":
        row.pop("UPDATE_DATE")
    elif kind == "future":
        row["UPDATE_DATE"] = "2027-01-01 00:00:00"
    elif kind == "outside_window":
        row.update({"REPORT_DATE": "2023-12-31", "NOTICE_DATE": "2024-04-17", "UPDATE_DATE": "2024-04-17"})
    else:
        row["OPERATE_INCOME"] = float("inf")
    source = EastmoneyCompanySourceFixture(_Session(row), monotonic=lambda: 0)
    period = date(2023, 12, 31) if kind == "outside_window" else _PERIOD
    with pytest.raises(ValueError):
        source.annual_income(CompanyAnnualIncomeRequest("600001", (period,)), observed_at=fixtures.NOW)


def test_missing_operating_revenue_stays_missing_instead_of_using_the_total():
    row = _report()
    row["OPERATE_INCOME"] = None
    source = EastmoneyCompanySourceFixture(_Session(row), monotonic=lambda: 0)
    assert source.annual_income(CompanyAnnualIncomeRequest("600001", (_PERIOD,)), observed_at=fixtures.NOW)[0].operating_revenue is None


def test_raw_ledger_formal_facts_and_restart_share_one_annual_reference(tmp_path):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    before = _read(index).positive_events[0]
    assert before.measurements.reference_report_date == _PERIOD
    assert before.measurements.reference_annual_revenue_cny is None
    requests = index.annual_income_requests(item.codes, fixtures.NOW)
    assert requests == (CompanyAnnualIncomeRequest("600001", (_PERIOD,)),)
    income = _income()
    assert index.commit_annual_income(requests[0], (income,), fixtures.NOW) == 1
    index.publish(item.codes, fixtures.NOW, changed_only=True)
    event = _read(index).positive_events[0]
    assert event.measurements.reference_annual_revenue_cny == _OPERATING_REVENUE
    assert event.measurements.reference_disclosed_at == _REPORTED
    assert event.measurements.amount_to_revenue_pct is None and event.measurements.amount_basis == "unknown"
    assert event.positive_level == before.positive_level == 0 and event.level_milestones == ()
    assert index.annual_income_requests(item.codes, fixtures.NOW) == ()
    with SqliteSession(index.facts_path) as formal:
        receipt = formal.run("SELECT source_key,record_hash,body_hash FROM company_input_receipt WHERE source='eastmoney_annual_income'").require_one()
        assert tuple(receipt) == (income.source_key, income.record_hash, None)
    restarted = fixtures._index(tmp_path)
    restarted.prepare_publication(item.codes, fixtures.NOW + timedelta(minutes=1))
    assert restarted.annual_income_requests(item.codes, fixtures.NOW + timedelta(minutes=1)) == ()
    assert _read(restarted).positive_events[0].measurements == event.measurements


@pytest.mark.parametrize("kind", ("later_revision", "wrong_year", "foreign_currency", "unobserved", "same_day", "missing", "conflict", "known_newer_version", "no_known_report"))
def test_contract_selection_does_not_rewrite_history_or_pick_a_convenient_version(tmp_path, kind):
    index = fixtures._index(tmp_path)
    _contract(index)
    event = _read(index).positive_events[0]
    incomes = (_income(),)
    financials = (_financial(),)
    if kind == "later_revision":
        incomes = (_income(updated_at=_SIGNED + timedelta(days=1)),)
    elif kind == "wrong_year":
        incomes = (_income(report_date=date(2024, 12, 31)),)
    elif kind == "foreign_currency":
        incomes = (_income(currency="USD"),)
    elif kind == "unobserved":
        incomes = (_income(observed_at=_SIGNED - timedelta(days=1)),)
    elif kind == "same_day":
        incomes = (_income(updated_at=_SIGNED),)
    elif kind == "missing":
        incomes = (_income(operating_revenue=None),)
    elif kind == "conflict":
        incomes += (_income(operating_revenue=1, record_hash="b" * 64),)
    elif kind == "known_newer_version":
        financials += (_financial(published_at=_REPORTED + timedelta(days=1)),)
    else:
        financials = ()
    assert SelectCompanyAnnualIncome("600001", event, incomes, fixtures.NOW, financials=financials) is None
    assert ApplyCompanyAnnualIncome("600001", (event,), incomes, fixtures.NOW, financials=financials) == (event,)


def test_no_known_annual_disclosure_does_not_trigger_new_network_requests(tmp_path):
    index = fixtures._index(tmp_path)
    item = _contract(index, known_annual=False)
    assert index.annual_income_requests(item.codes, fixtures.NOW) == ()


def test_cached_source_older_than_known_annual_version_cannot_end_the_recovery(tmp_path):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    request = CompanyAnnualIncomeRequest("600001", (_PERIOD,))
    earlier = _REPORTED - timedelta(days=1)
    index.commit_annual_income(request, (_income(disclosed_at=earlier, updated_at=earlier),), fixtures.NOW)
    index.publish(item.codes, fixtures.NOW, changed_only=True)
    assert _read(index).positive_events[0].measurements.reference_annual_revenue_cny is None
    assert index.annual_income_requests(item.codes, fixtures.NOW) == (request,)


@pytest.mark.parametrize("reference", ("公司营业收入", "公司2024年度营业收入及2025年度营业收入", "公司2025年度营业总收入"))
def test_ambiguous_or_unstated_reference_year_does_not_guess_an_annual_report(tmp_path, reference):
    index = fixtures._index(tmp_path)
    item = _contract(index, reference=reference)
    assert _read(index).positive_events[0].measurements.reference_report_date is None
    assert index.annual_income_requests(item.codes, fixtures.NOW) == ()


def test_source_cancellation_after_page_does_not_fetch_the_report():
    cancelled = Event()

    class Session(_Session):
        def get(self, *args, **kwargs):
            response = super().get(*args, **kwargs)
            cancelled.set()
            return response

    session = Session()
    source = EastmoneyCompanySourceFixture(session, monotonic=lambda: 0, cancel_requested=cancelled.is_set)
    with pytest.raises(HttpCancelled):
        source.annual_income(CompanyAnnualIncomeRequest("600001", (_PERIOD,)), observed_at=fixtures.NOW)
    assert len(session.requests) == 1


def test_rejected_multi_report_commit_does_not_keep_partial_inputs(tmp_path):
    index = fixtures._index(tmp_path)
    _contract(index)
    request = CompanyAnnualIncomeRequest("600001", (date(2024, 12, 31), _PERIOD))
    with pytest.raises(ValueError, match="company_annual_income_disclosure_unavailable"):
        index.commit_annual_income(request, (_income(report_date=date(2024, 12, 31)), _income()), fixtures.NOW)
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM annual_income").require_one()[0] == 0


class _Sources(fixtures.Sources):
    def __init__(self, item, *, fail=False):
        super().__init__()
        self.item, self.fail = item, fail
        self.annual_calls = 0

    def fetch(self, query, **kwargs):
        if isinstance(query, DisclosurePartition):
            present = query.market == "sse" and query.start <= self.item.published_at.date() <= query.end
            return DisclosurePage((self.item,) if present else (), int(present), False)
        return super().fetch(query, **kwargs)

    def annual_income(self, request, *, observed_at):
        self.annual_calls += 1
        if self.fail:
            raise HttpTimeout("fixture_timeout")
        return (_income(observed_at=observed_at),)


def test_real_sync_fetches_only_needed_annual_report_and_reuses_it_on_restart(tmp_path):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    source = _Sources(item)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), item.codes, fixtures.NOW
    )
    assert source.annual_calls == 1 and not any(failure.startswith("annual_income:") for failure in result.failures)
    assert _read(index).positive_events[0].measurements.reference_annual_revenue_cny == _OPERATING_REVENUE
    restarted = fixtures._index(tmp_path)
    fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(restarted, source, source, lambda: 0), item.codes, fixtures.NOW + timedelta(minutes=1)
    )
    assert source.annual_calls == 1


def test_annual_source_failure_keeps_local_facts_and_does_not_retry_each_publication(tmp_path):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    source = _Sources(item, fail=True)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0), item.codes, fixtures.NOW
    )
    assert source.annual_calls == 1
    assert "annual_income:timeout" in result.failures and result.published_codes == 1
    assert _read(index).positive_events[0].measurements.reference_annual_revenue_cny is None


def test_cancellation_during_annual_fetch_keeps_publication_and_stops_database_work(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    cancelled = Event()
    published_count = index.published_count

    def checked_count(*args):
        assert not cancelled.is_set(), "cancellation must not reopen the formal database"
        return published_count(*args)

    class Source(_Sources):
        def annual_income(self, request, *, observed_at):
            incomes = super().annual_income(request, observed_at=observed_at)
            cancelled.set()
            return incomes

    monkeypatch.setattr(index, "published_count", checked_count)
    source = Source(item)
    result = fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(index, source, source, lambda: 0, cancel_requested=cancelled.is_set),
        item.codes, fixtures.NOW,
    )
    assert result.failures == ("task:cancelled",) and result.published_codes == 1
    with SqliteSession(index.path) as connection:
        assert connection.run("SELECT count(*) FROM annual_income").require_one()[0] == 0
    assert _read(index).positive_events[0].measurements.reference_annual_revenue_cny is None


def test_durable_annual_input_recovers_after_formal_publication_failure_without_network(tmp_path, monkeypatch):
    index = fixtures._index(tmp_path)
    item = _contract(index)
    source = _Sources(item)
    publish = index.publish

    def failing_publish(*args, **kwargs):
        if source.annual_calls:
            raise RuntimeError("fixture_formal_crash")
        return publish(*args, **kwargs)

    monkeypatch.setattr(index, "publish", failing_publish)
    with pytest.raises(RuntimeError, match="fixture_formal_crash"):
        fixtures._sync_company_evidence(
            CompanyEvidenceDependencies(index, source, source, lambda: 0), item.codes, fixtures.NOW
        )
    assert _read(index).positive_events[0].measurements.reference_annual_revenue_cny is None
    restarted = fixtures._index(tmp_path)
    fixtures._sync_company_evidence(
        CompanyEvidenceDependencies(restarted, source, source, lambda: 0), item.codes, fixtures.NOW + timedelta(minutes=1)
    )
    assert source.annual_calls == 1
    assert _read(restarted).positive_events[0].measurements.reference_annual_revenue_cny == _OPERATING_REVENUE
