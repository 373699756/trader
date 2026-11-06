"""Bounded supplier pages decode into typed, still unqualified observations."""

from copy import deepcopy
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_structured_fields import (
    CompanyStructuredDataset,
    CompanyStructuredReason,
    ResolveCompanyStructuredFields,
)
from trader.download.infra.company_structured_fields import DecodeCompanyStructuredFields

AT = datetime(2026, 11, 5, 15, tzinfo=ZoneInfo("Asia/Shanghai"))


def _payload(dataset):
    rows = {
        CompanyStructuredDataset.REPURCHASE: {
            "DIM_SCODE": "600001", "UPDATEDATE": "2026-11-05", "DIM_TRADEDATE": "2026-11-04",
            "REPURCODE": "16785", "REPURNUM": None, "REPURAMOUNT": None,
            "REPUROBJECTIVE": "本次回购的股份将全部予以注销", "REPURPROGRESS": "002",
            "ZSZXX": 0.5, "ZSZSX": 1.0,
        },
        CompanyStructuredDataset.HOLDER_TRADE: {
            "SECURITY_CODE": "600001", "NOTICE_DATE": "2026-11-05", "HOLDER_NAME": "股东",
            "DIRECTION": "减持", "CHANGE_NUM_SYMBOL": -18.24, "AFTER_CHANGE_RATE": 0.06247466719,
            "CHANGE_FREE_RATIO": 0.07, "HOLD_RATIO": 6.97,
            "START_DATE": "2026-09-21", "END_DATE": "2026-09-21",
        },
        CompanyStructuredDataset.CONTRACT: {
            "SECURITYCODE": "600001", "DIM_RDATE": "2026-11-05", "UPDATEDATE": "2026-11-05",
            "AMOUNTS": 22409041.99, "SNDYYSR": None, "OPERATEREVE": 996855389.76,
            "SIGNATORY": "发行人", "SIGNATORYRELNAME": "公司本身", "SIGNDATE": None,
            "SIGNEFFECT": "本次中标总金额为23,625.203107万元", "ISABOLISHED": None,
            "YEAR": "2026", "CONTRACTNAME": "中标通知书", "COUNTERPARTY": "招标人",
        },
    }
    return {"success": True, "code": 0, "result": {"count": 1, "pages": 1, "data": [rows[dataset]]}}


def _decode(dataset, payload=None, **kwargs):
    return DecodeCompanyStructuredFields(
        dataset, payload or _payload(dataset), observed_at=AT,
        window_start=date(2025, 11, 5), window_end=date(2026, 11, 5), **kwargs,
    )


def test_repurchase_provider_key_is_not_an_official_announcement_identity_or_actual_ratio():
    page = _decode(CompanyStructuredDataset.REPURCHASE)
    row = page.observations[0]
    assert row.provider_event_id == "16785" and row.measurements.planned_ratio_min_pct == 0.5
    result = ResolveCompanyStructuredFields((row,), AT)[0]
    assert result.reason is CompanyStructuredReason.EVENT_IDENTITY_UNVERIFIED


def test_holder_signed_quantity_is_converted_from_ten_thousand_shares_without_using_holding_or_float_ratio():
    row = _decode(CompanyStructuredDataset.HOLDER_TRADE).observations[0]
    assert row.measurements.change_shares == pytest.approx(-182400)
    assert row.measurements.total_capital_ratio_pct == 0.06247466719
    assert row.updated_at is None
    assert ResolveCompanyStructuredFields((row,), AT)[0].reason is CompanyStructuredReason.POINT_IN_TIME_UNVERIFIED


def test_contract_line_item_and_effect_aggregate_are_preserved_as_distinct_unqualified_inputs():
    row = _decode(CompanyStructuredDataset.CONTRACT).observations[0]
    assert row.measurements.amount_candidate == 22409041.99
    assert row.measurements.annual_income_candidate is None
    assert row.measurements.effect_text == "本次中标总金额为23,625.203107万元"
    assert row.provider_event_id is None
    assert ResolveCompanyStructuredFields((row,), AT)[0].reason is CompanyStructuredReason.EVENT_IDENTITY_UNVERIFIED


def test_population_filter_preserves_physical_response_count_and_precise_hash():
    page = _decode(CompanyStructuredDataset.CONTRACT, eligible_codes=("600002",), response_hash="b" * 64)
    assert page.raw_count == 1 and not page.observations and page.response_hash == "b" * 64


def test_row_hash_binds_unknown_supplier_version_fields_without_exposing_raw_payload():
    dataset = CompanyStructuredDataset.CONTRACT
    first = _decode(dataset).observations[0]
    payload = _payload(dataset)
    payload["result"]["data"][0]["UNRELATED_PROVIDER_FIELD"] = "private-payload"
    second = _decode(dataset, payload).observations[0]
    assert first.record_hash != second.record_hash
    assert "private-payload" not in repr(second)


@pytest.mark.parametrize("field,value", [("AMOUNTS", True), ("AMOUNTS", float("inf")), ("AMOUNTS", -1), ("YEAR", "unknown")])
def test_invalid_measurements_never_become_values(field, value):
    payload = _payload(CompanyStructuredDataset.CONTRACT)
    payload["result"]["data"][0][field] = value
    with pytest.raises(ValueError):
        _decode(CompanyStructuredDataset.CONTRACT, payload)


def test_date_filter_ignored_empty_pages_and_invalid_pagination_are_checked():
    dataset = CompanyStructuredDataset.CONTRACT
    payload = _payload(dataset)
    payload["result"]["data"][0]["DIM_RDATE"] = "2025-11-04"
    with pytest.raises(ValueError, match="date_filter_ignored"):
        _decode(dataset, payload)
    payload = {"success": True, "code": 0, "result": {"count": 0, "pages": 0, "data": None}}
    assert not _decode(dataset, payload).observations
    payload = _payload(dataset)
    payload["result"]["pages"] = 0
    with pytest.raises(ValueError, match="page_invalid"):
        _decode(dataset, payload)
    payload = _payload(dataset)
    payload["result"]["count"] = 501
    with pytest.raises(ValueError, match="page_invalid"):
        _decode(dataset, payload)


def test_duplicate_population_and_too_many_returned_rows_are_rejected():
    dataset = CompanyStructuredDataset.CONTRACT
    with pytest.raises(ValueError, match="population_invalid"):
        _decode(dataset, eligible_codes=("600001", "600001"))
    payload = _payload(dataset)
    payload["result"]["data"] = deepcopy(payload["result"]["data"]) * 2
    with pytest.raises(ValueError, match="rows_invalid"):
        _decode(dataset, payload, page_size=1)
