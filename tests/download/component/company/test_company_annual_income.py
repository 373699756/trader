

from datetime import date, timedelta

from threading import Event


import pytest

from tests.helpers.download.company import company_evidence as fixtures

from tests.helpers.download.company_resources import EastmoneyCompanySourceFixture


from trader.download.domain.company_annual_income import ApplyCompanyAnnualIncome, CompanyAnnualIncomeRequest, SelectCompanyAnnualIncome



from trader.download.domain.company_sync import CompanySourceCancelled


from tests.helpers.download.company.company_annual_income import (
    _OPERATING_REVENUE,
    _PERIOD,
    _REPORTED,
    _SIGNED,
    _Session,
    _TOTAL_OPERATING_INCOME,
    _contract,
    _financial,
    _income,
    _read,
    _report,
)


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








def test_source_cancellation_after_page_does_not_fetch_the_report():
    cancelled = Event()

    class Session(_Session):
        def get(self, *args, **kwargs):
            response = super().get(*args, **kwargs)
            cancelled.set()
            return response

    session = Session()
    source = EastmoneyCompanySourceFixture(session, monotonic=lambda: 0, cancel_requested=cancelled.is_set)
    with pytest.raises(CompanySourceCancelled):
        source.annual_income(CompanyAnnualIncomeRequest("600001", (_PERIOD,)), observed_at=fixtures.NOW)
    assert len(session.requests) == 1
