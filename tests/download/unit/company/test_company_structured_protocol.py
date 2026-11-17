"""Production structured inputs reject rejected, contradictory, or misbound pages."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.helpers.download.company.company_sources import NOW, Session
from trader.download.domain.company_evidence import StructuredCompanyQuery
from trader.download.infra.company_structured_source import DecodeCompanyFinancialPage, EastmoneyCompanySource


def _row(**changes):
    return {"SECURITY_CODE": "600001", "TRADE_DATE": NOW.date().isoformat(), "PLEDGE_RATIO": 0, **changes}


def _payload(rows, *, total=None, pages=1, **changes):
    return {"code": 0, "success": True, "result": {"count": len(rows) if total is None else total, "pages": pages, "data": rows}, **changes}


def _fetch(payload, component="pledge"):
    session = Session(payload)
    page = EastmoneyCompanySource(session, monotonic=lambda: 0, timeout_seconds=8).fetch(
        StructuredCompanyQuery(component, NOW.date(), NOW.date()), observed_at=NOW,
    )
    assert len(session.requests) == 1
    return page


@pytest.mark.parametrize("changes", (
    {"success": False}, {"code": 9501}, {"code": False}, {"code": 0.0}, {"success": 1}, {"success": None},
))
@pytest.mark.parametrize("empty", (False, True))
def test_rejected_provider_envelope_is_never_a_valid_page_or_zero_snapshot(changes, empty):
    payload = _payload([] if empty else [_row()], **changes)
    with pytest.raises(ValueError, match="structured_source_not_accepted"):
        _fetch(payload)


@pytest.mark.parametrize("field", ("code", "success"))
def test_provider_success_fields_are_required(field):
    payload = _payload([_row()])
    payload.pop(field)
    with pytest.raises(ValueError, match="structured_source_not_accepted"):
        _fetch(payload)


@pytest.mark.parametrize("rows,total,pages", (
    ([], 1, 1), ([_row()], 0, 0), ([], 0, 2), ([_row()], 1, 0), ([_row()], 1, 2),
    ([_row()], 2, 1), ([_row()], 501, 1), ([_row()] * 501, 501, 2),
))
def test_contradictory_or_oversized_page_does_not_reach_a_normalized_result(rows, total, pages):
    with pytest.raises(ValueError, match="structured_source_pagination_invalid"):
        _fetch(_payload(rows, total=total, pages=pages))


@pytest.mark.parametrize("pages", (0, 1))
def test_successful_native_empty_page_remains_an_empty_query_not_a_row(pages):
    page = _fetch(_payload([], pages=pages))
    assert page.row_count == page.total == 0 and page.page == page.pages == 1 and page.ratios == ()


@pytest.mark.parametrize("changes,reason", (
    ({"SECURITY_CODE": 600001}, "company_source_code_invalid"),
    ({"SECURITY_CODE": "６００００１"}, "company_source_code_invalid"),
    ({"SECURITY_CODE": "60001"}, "company_source_code_invalid"),
    ({"SECUCODE": "600001.SZ"}, "company_source_identity_invalid"),
    ({"SECUCODE": "000001.SZ"}, "company_source_identity_invalid"),
    ({"SECUCODE": None}, "company_source_identity_invalid"),
))
def test_security_identity_cannot_be_coerced_or_assigned_to_another_market(changes, reason):
    with pytest.raises(ValueError, match=reason):
        _fetch(_payload([_row(**changes)]))


@pytest.mark.parametrize("day", (None, 20261018, "2026-10-18 garbage", "2026-10-18 99:00:00", "20261018", "2026-02-30"))
def test_date_validation_consumes_the_entire_native_value(day):
    with pytest.raises(ValueError, match="company_source_date_invalid"):
        _fetch(_payload([_row(TRADE_DATE=day)]))


def test_timezone_aware_native_timestamp_uses_the_shanghai_business_date():
    page = _fetch(_payload([_row(TRADE_DATE="2026-10-17T16:00:00+00:00")]))
    assert page.ratios[0].day == NOW.date()


@pytest.mark.parametrize("component", ("pledge", "unlock"))
def test_duplicate_normalized_ratio_keys_are_not_summed_or_silently_kept(component):
    row = _row() if component == "pledge" else {"SECURITY_CODE": "600001", "FREE_DATE": NOW.date().isoformat(), "TOTAL_RATIO": 0}
    with pytest.raises(ValueError, match="company_ratio_duplicate"):
        _fetch(_payload([row, row]), component)


@pytest.mark.parametrize("ratio,expected", ((0, 0), (0.031, 3.1), (1, 100)))
def test_unlock_native_fraction_preserves_the_existing_percentage_point_conversion(ratio, expected):
    page = _fetch(_payload([{"SECURITY_CODE": "600001", "FREE_DATE": NOW.date().isoformat(), "TOTAL_RATIO": ratio}]), "unlock")
    assert page.ratios[0].ratio_pct == pytest.approx(expected)


@pytest.mark.parametrize("ratio", (-0.01, 1.01, None))
def test_unlock_missing_or_invalid_fraction_does_not_become_zero_or_a_valid_ratio(ratio):
    with pytest.raises(ValueError, match="company_ratio_(?:out_of_range|invalid)"):
        _fetch(_payload([{"SECURITY_CODE": "600001", "FREE_DATE": NOW.date().isoformat(), "TOTAL_RATIO": ratio}]), "unlock")


def test_financial_row_cross_checks_the_supplied_exchange_and_security_code():
    query = StructuredCompanyQuery("financial", NOW.date(), NOW.date())
    for fields in ({"SECUCODE": "600001.SZ"}, {"SECUCODE": "600001.SH", "SECURITY_CODE": "000001"}):
        with pytest.raises(ValueError, match="company_source_identity_invalid"):
            DecodeCompanyFinancialPage(_payload([_financial_row(**fields)]),
                                       query, observed_at=NOW)


def _financial_row(**changes):
    return {
        "SECUCODE": "600001.SH", "REPORT_DATE": "2026-09-30", "NOTICE_DATE": NOW.date().isoformat(),
        "EPSJB": 0, "BPS": None, "TOTALOPERATEREVETZ": -1, "PARENTNETPROFITTZ": None,
        "KCFJCXSYJLRTZ": None, "ROEJQ": None, "PARENTNETPROFIT": -2, "KCFJCXSYJLR": 0, **changes,
    }


@pytest.mark.parametrize("field", (
    "SECUCODE", "REPORT_DATE", "NOTICE_DATE", "EPSJB", "BPS", "TOTALOPERATEREVETZ", "PARENTNETPROFITTZ",
    "KCFJCXSYJLRTZ", "ROEJQ", "PARENTNETPROFIT", "KCFJCXSYJLR",
))
def test_financial_missing_columns_cannot_become_source_null_or_excluded_rows(field):
    row = _financial_row()
    row.pop(field)
    with pytest.raises(ValueError, match="financial_source_fields_missing"):
        _fetch(_payload([row]), "financial")


@pytest.mark.parametrize("notice", (
    "2026-10-18", "2026-10-18 09:15:00", "2026-10-18T01:15:00+00:00",
))
def test_financial_date_precision_and_accepted_explicit_times_keep_day_projection(notice):
    page = _fetch(_payload([_financial_row(NOTICE_DATE=notice)]), "financial")
    report, = page.financials
    assert report.published_at == NOW.replace(hour=0, minute=0)
    assert report.basic_eps == report.core_net_profit == 0 and report.book_value_per_share is None
    assert report.revenue_growth_pct == -1 and report.parent_net_profit == -2


@pytest.mark.parametrize("notice", ("2026-10-18 09:15:01", "2026-10-18T01:15:01+00:00"))
def test_financial_explicit_future_time_is_not_hidden_by_date_projection(notice):
    with pytest.raises(ValueError, match="financial_date_filter_ignored"):
        _fetch(_payload([_financial_row(NOTICE_DATE=notice)]), "financial")


def test_financial_year_floor_uses_shanghai_date_for_aware_caller():
    observed_at = datetime(2026, 10, 17, 17, tzinfo=timezone.utc)
    lower = observed_at.astimezone(NOW.tzinfo).date() - timedelta(days=365)
    query = StructuredCompanyQuery("financial", lower - timedelta(days=1), NOW.date())
    session = Session(_payload([_financial_row(NOTICE_DATE=(lower - timedelta(days=1)).isoformat())]))
    with pytest.raises(ValueError, match="financial_date_filter_ignored"):
        EastmoneyCompanySource(session, monotonic=lambda: 0, timeout_seconds=8).fetch(query, observed_at=observed_at)
    assert f"NOTICE_DATE>='{lower}'" in session.requests[0]["params"]["filter"]


def test_financial_naive_observation_is_rejected_before_request():
    session = Session(_payload([_financial_row()]))
    with pytest.raises(ValueError, match="company_financial_observation_time_invalid"):
        EastmoneyCompanySource(session, monotonic=lambda: 0, timeout_seconds=8).fetch(
            StructuredCompanyQuery("financial", NOW.date(), NOW.date()), observed_at=NOW.replace(tzinfo=None),
        )
    assert session.requests == []


def test_latest_pledge_date_discovery_is_one_row_and_not_a_full_page_request():
    session = Session(_payload([{"TRADE_DATE": NOW.date().isoformat()}], total=113824, pages=113824))
    source = EastmoneyCompanySource(session, monotonic=lambda: 0, timeout_seconds=8)
    assert source.pledge_day(NOW) == NOW.date()
    assert len(session.requests) == 1 and session.requests[0]["params"]["pageSize"] == "1"
    session = Session(_payload([{"TRADE_DATE": NOW.date().isoformat()}] * 2))
    with pytest.raises(ValueError, match="pledge_snapshot_rows_invalid"):
        EastmoneyCompanySource(session, monotonic=lambda: 0, timeout_seconds=8).pledge_day(NOW)
