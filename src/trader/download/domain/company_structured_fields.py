"""Typed supplier observations and missing-evidence conclusions for field recovery."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from trader.download.domain.company_evidence import EvidenceMarket


class CompanyStructuredDataset(str, Enum):
    REPURCHASE = "repurchase"
    HOLDER_TRADE = "holder_trade"
    CONTRACT = "contract"


class CompanyStructuredReason(str, Enum):
    EVENT_IDENTITY_UNVERIFIED = "event_identity_unverified"
    POINT_IN_TIME_UNVERIFIED = "point_in_time_unverified"
    SOURCE_AFTER_CUTOFF = "source_after_cutoff"


@dataclass(frozen=True, slots=True)
class CompanyRepurchaseObservation:
    quantity_candidate: float | None
    amount_candidate: float | None
    purpose_text: str | None
    progress_code: str | None
    planned_ratio_min_pct: float | None
    planned_ratio_max_pct: float | None

    def __post_init__(self) -> None:
        _numbers(self.quantity_candidate, self.amount_candidate, self.planned_ratio_min_pct, self.planned_ratio_max_pct)
        _labels(self.purpose_text, self.progress_code)


@dataclass(frozen=True, slots=True)
class CompanyHolderTradeObservation:
    holder_name: str | None
    direction: str | None
    change_shares: float | None
    total_capital_ratio_pct: float | None
    started_on: date | None
    ended_on: date | None

    def __post_init__(self) -> None:
        _labels(self.holder_name, self.direction)
        _numbers(self.total_capital_ratio_pct)
        if self.change_shares is not None and (
            isinstance(self.change_shares, bool) or not math.isfinite(self.change_shares)
        ):
            raise ValueError("company_structured_quantity_invalid")
        if self.started_on and self.ended_on and self.started_on > self.ended_on:
            raise ValueError("company_structured_period_invalid")


@dataclass(frozen=True, slots=True)
class CompanyContractObservation:
    amount_candidate: float | None
    annual_income_candidate: float | None
    signatory: str | None
    signatory_relation: str | None
    contract_name: str | None
    counterparty: str | None
    effect_text: str | None
    cancellation_text: str | None
    signed_on: date | None
    reported_year: int | None

    def __post_init__(self) -> None:
        _numbers(self.amount_candidate, self.annual_income_candidate)
        _labels(
            self.signatory, self.signatory_relation, self.contract_name, self.counterparty,
            self.effect_text, self.cancellation_text,
        )
        if self.reported_year is not None and (
            type(self.reported_year) is not int or not 1900 <= self.reported_year <= 9999
        ):
            raise ValueError("company_structured_year_invalid")


CompanyStructuredMeasurements = CompanyRepurchaseObservation | CompanyHolderTradeObservation | CompanyContractObservation


@dataclass(frozen=True, slots=True)
class CompanyStructuredObservation:
    dataset: CompanyStructuredDataset
    code: str
    provider_event_id: str | None
    disclosed_at: datetime | None
    updated_at: datetime | None
    observed_at: datetime
    record_hash: str
    measurements: CompanyStructuredMeasurements

    def __post_init__(self) -> None:
        EvidenceMarket(self.code)
        _hash(self.record_hash)
        _labels(self.provider_event_id)
        _times(self.disclosed_at, self.updated_at, self.observed_at)
        expected = {
            CompanyStructuredDataset.REPURCHASE: CompanyRepurchaseObservation,
            CompanyStructuredDataset.HOLDER_TRADE: CompanyHolderTradeObservation,
            CompanyStructuredDataset.CONTRACT: CompanyContractObservation,
        }[self.dataset]
        if not isinstance(self.measurements, expected):
            raise ValueError("company_structured_measurement_type_invalid")
        # A missing original date or a revision before that date remains an
        # unverified observation. It is never repaired by inventing timestamps.


@dataclass(frozen=True, slots=True)
class CompanyStructuredPage:
    dataset: CompanyStructuredDataset
    page: int
    page_size: int
    reported_pages: int
    reported_count: int
    raw_count: int
    observations: tuple[CompanyStructuredObservation, ...]
    response_hash: str

    def __post_init__(self) -> None:
        _hash(self.response_hash)
        if (
            any(type(value) is not int for value in (
                self.page, self.page_size, self.reported_pages, self.reported_count, self.raw_count,
            ))
            or not 1 <= self.page_size <= 500 or self.page < 1
            or self.reported_pages < 0 or self.reported_count < 0
            or self.raw_count < 0 or self.raw_count > self.page_size
            or self.raw_count > self.reported_count or len(self.observations) > self.raw_count
            or self.reported_count > 0 and self.reported_pages < self.page
            or self.reported_count > self.reported_pages * self.page_size
            or self.reported_count > 0 and self.raw_count == 0
            or self.reported_count == 0 and (self.reported_pages not in (0, 1) or self.raw_count != 0)
            or any(row.dataset is not self.dataset for row in self.observations)
        ):
            raise ValueError("company_structured_page_invalid")


@dataclass(frozen=True, slots=True)
class CompanyStructuredResolution:
    """A missing-evidence conclusion, never a field value or completion receipt."""

    observation: CompanyStructuredObservation
    reason: CompanyStructuredReason


def ResolveCompanyStructuredFields(
    observations: tuple[CompanyStructuredObservation, ...],
    observed_at: datetime,
) -> tuple[CompanyStructuredResolution, ...]:
    """Classify why the currently sampled sources cannot supply formal fields.

    The sources have no verified official-document/event mapping. Numeric
    availability and supplier plan keys cannot establish that mapping, subject
    eligibility, cumulative basis or the original point-in-time version. An
    observation remains a clue even when all its supplier values are present.
    """
    _times(observed_at)
    return tuple(
        CompanyStructuredResolution(observation, _observation_reason(observation, observed_at))
        for observation in observations
    )


def _observation_reason(observation: CompanyStructuredObservation, observed_at: datetime) -> CompanyStructuredReason:
    if observation.observed_at > observed_at or any(
        at is not None and at > observed_at for at in (observation.disclosed_at, observation.updated_at)
    ):
        return CompanyStructuredReason.SOURCE_AFTER_CUTOFF
    if (
        observation.disclosed_at is None or observation.updated_at is None
        or observation.updated_at < observation.disclosed_at
    ):
        return CompanyStructuredReason.POINT_IN_TIME_UNVERIFIED
    return CompanyStructuredReason.EVENT_IDENTITY_UNVERIFIED


def _hash(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("company_structured_hash_invalid")


def _times(*values: datetime | None) -> None:
    if any(value is not None and value.utcoffset() is None for value in values):
        raise ValueError("company_structured_time_invalid")


def _numbers(*values: float | None) -> None:
    if any(value is not None and (isinstance(value, bool) or not math.isfinite(value) or value < 0) for value in values):
        raise ValueError("company_structured_number_invalid")


def _labels(*values: str | None) -> None:
    if any(value is not None and (not value or len(value) > 4000) for value in values):
        raise ValueError("company_structured_label_invalid")


__all__ = [
    "CompanyContractObservation",
    "CompanyHolderTradeObservation",
    "CompanyRepurchaseObservation",
    "CompanyStructuredDataset",
    "CompanyStructuredMeasurements",
    "CompanyStructuredObservation",
    "CompanyStructuredPage",
    "CompanyStructuredReason",
    "CompanyStructuredResolution",
    "ResolveCompanyStructuredFields",
]
