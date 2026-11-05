"""Typed annual operating revenue and conservative contract reference selection."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import date, datetime

from trader.download.domain.company_events import CompanyPositiveEvent, ContractMeasurements
from trader.download.domain.company_evidence import CompanyFinancial, EvidenceMarket


@dataclass(frozen=True, slots=True)
class CompanyAnnualIncomeRequest:
    code: str
    report_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if (
            not 1 <= len(self.report_dates) <= 5
            or tuple(sorted(set(self.report_dates))) != self.report_dates
            or any((day.month, day.day) != (12, 31) for day in self.report_dates)
        ):
            raise ValueError("company_annual_income_request_invalid")


@dataclass(frozen=True, slots=True)
class CompanyAnnualIncome:
    code: str
    report_date: date
    disclosed_at: datetime
    updated_at: datetime
    observed_at: datetime
    currency: str
    operating_revenue: float | None
    record_hash: str

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        if (
            (self.report_date.month, self.report_date.day) != (12, 31)
            or any(at.utcoffset() is None for at in (self.disclosed_at, self.updated_at, self.observed_at))
            or self.report_date >= self.disclosed_at.date()
            or not self.disclosed_at <= self.updated_at <= self.observed_at
            or not self.currency
            or len(self.record_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.record_hash)
            or self.operating_revenue is not None and (
                isinstance(self.operating_revenue, bool)
                or not math.isfinite(self.operating_revenue) or self.operating_revenue < 0
            )
        ):
            raise ValueError("company_annual_income_invalid")

    @property
    def source_key(self) -> str:
        return f"{self.code}:{self.report_date.isoformat()}:{self.disclosed_at.isoformat()}:{self.updated_at.isoformat()}"


def SelectCompanyAnnualIncome(
    code: str,
    event: CompanyPositiveEvent,
    incomes: tuple[CompanyAnnualIncome, ...],
    observed_at: datetime,
    *,
    financials: tuple[CompanyFinancial, ...],
) -> CompanyAnnualIncome | None:
    """Use a current observation of a version already disclosed before this contract.

    A later revision cannot be substituted into an earlier contract. Conflicting
    versions at the same source time, a missing value and another currency all
    remain unknown rather than selecting a more convenient older value.
    """
    measurement = event.measurements
    if not isinstance(measurement, ContractMeasurements) or measurement.reference_report_date is None:
        return None
    known = tuple(
        row.published_at for row in financials
        if row.code == code and row.report_date == measurement.reference_report_date
        and row.published_at.date() < event.first_disclosed_at.date()
    )
    if not known:
        return None
    latest_known = max(known)
    candidates = tuple(
        row for row in incomes
        if row.code == code and event.first_disclosed_at <= row.observed_at <= observed_at
        and row.updated_at.date() < event.first_disclosed_at.date()
        and row.updated_at >= latest_known
        and row.report_date == measurement.reference_report_date
    )
    if not candidates:
        return None
    latest = max((row.report_date, row.updated_at, row.disclosed_at) for row in candidates)
    versions = tuple(row for row in candidates if (row.report_date, row.updated_at, row.disclosed_at) == latest)
    if len({(row.currency, row.operating_revenue) for row in versions}) != 1:
        return None
    income = versions[0]
    return income if income.currency == "CNY" and income.operating_revenue is not None else None


def ApplyCompanyAnnualIncome(
    code: str,
    events: tuple[CompanyPositiveEvent, ...],
    incomes: tuple[CompanyAnnualIncome, ...],
    observed_at: datetime,
    *,
    financials: tuple[CompanyFinancial, ...],
) -> tuple[CompanyPositiveEvent, ...]:
    """Fill annual reference facts; tax comparability and scoring eligibility stay separate."""
    results: list[CompanyPositiveEvent] = []
    for event in events:
        measurement = event.measurements
        if not isinstance(measurement, ContractMeasurements) or (
            measurement.reference_annual_revenue_cny is not None
            and measurement.reference_disclosed_at is not None
        ):
            results.append(event)
            continue
        income = SelectCompanyAnnualIncome(code, event, incomes, observed_at, financials=financials)
        if income is None or (
            measurement.reference_annual_revenue_cny is not None
            and measurement.reference_annual_revenue_cny != income.operating_revenue
        ):
            results.append(event)
            continue
        results.append(replace(event, measurements=replace(
            measurement,
            reference_annual_revenue_cny=income.operating_revenue,
            reference_report_date=income.report_date,
            reference_disclosed_at=income.disclosed_at,
        )))
    return tuple(results)


__all__ = [
    "ApplyCompanyAnnualIncome",
    "CompanyAnnualIncome",
    "CompanyAnnualIncomeRequest",
    "SelectCompanyAnnualIncome",
]
