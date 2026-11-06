"""Supplier timestamps and plan keys do not establish official event identity."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.company_structured_fields import (
    CompanyHolderTradeObservation,
    CompanyStructuredDataset,
    CompanyStructuredObservation,
    CompanyStructuredReason,
    ResolveCompanyStructuredFields,
)

AT = datetime(2026, 11, 5, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
DISCLOSED = AT - timedelta(days=1)


def _observation():
    return CompanyStructuredObservation(
        CompanyStructuredDataset.HOLDER_TRADE, "600001", None, DISCLOSED, DISCLOSED, AT, "a" * 64,
        CompanyHolderTradeObservation("股东", "增持", 10000, 0.5, date(2026, 11, 1), date(2026, 11, 4)),
    )


def _resolve(observation):
    return ResolveCompanyStructuredFields((observation,), AT)[0]


def test_same_code_date_and_holder_name_still_require_original_event_evidence():
    observation = _observation()
    result = _resolve(observation)
    assert result.reason is CompanyStructuredReason.EVENT_IDENTITY_UNVERIFIED
    assert result.observation == observation


@pytest.mark.parametrize("changes", [
    {"updated_at": None}, {"disclosed_at": None}, {"updated_at": DISCLOSED - timedelta(days=1)},
])
def test_missing_original_revision_is_explicitly_unknown(changes):
    assert _resolve(replace(_observation(), **changes)).reason is CompanyStructuredReason.POINT_IN_TIME_UNVERIFIED


@pytest.mark.parametrize("changes", [
    {"observed_at": AT + timedelta(seconds=1)}, {"updated_at": AT + timedelta(seconds=1)},
    {"disclosed_at": AT + timedelta(seconds=1)},
])
def test_observations_after_the_cutoff_cannot_supply_historical_fields(changes):
    assert _resolve(replace(_observation(), **changes)).reason is CompanyStructuredReason.SOURCE_AFTER_CUTOFF


def test_latest_nonempty_supplier_value_is_not_a_verified_replacement():
    original = _observation()
    newer = replace(original, record_hash="b" * 64, updated_at=AT,
                    measurements=replace(original.measurements, total_capital_ratio_pct=0.7))
    results = ResolveCompanyStructuredFields((original, newer), AT)
    assert len(results) == 2
    assert {row.reason for row in results} == {CompanyStructuredReason.EVENT_IDENTITY_UNVERIFIED}
    assert results[0].observation.measurements.total_capital_ratio_pct == 0.5


def test_nan_naive_time_and_wrong_measurement_dataset_are_rejected():
    with pytest.raises(ValueError, match="number_invalid"):
        replace(_observation().measurements, total_capital_ratio_pct=float("nan"))
    with pytest.raises(ValueError, match="time_invalid"):
        replace(_observation(), observed_at=AT.replace(tzinfo=None))
    with pytest.raises(ValueError, match="measurement_type_invalid"):
        replace(_observation(), dataset=CompanyStructuredDataset.CONTRACT)
