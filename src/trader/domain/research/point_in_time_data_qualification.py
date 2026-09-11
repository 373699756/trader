"""Typed, fail-closed qualification for historical point-in-time research data."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from trader.domain.research.artifact_identity import canonical_artifact_hash

QualificationState = Literal["qualified", "historical_data_insufficient"]

_HASH = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True)
class DailyArchiveQualification:
    sessions: int
    universe_count: int
    completed_codes: int
    failed_codes: int
    coverage_status: str
    manifest_hash: str
    state: QualificationState
    failure_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        counts = (self.sessions, self.universe_count, self.completed_codes, self.failed_codes)
        reasons = _reasons(self.failure_reasons)
        if (
            any(isinstance(value, bool) or value < 0 for value in counts)
            or self.completed_codes > self.universe_count
            or self.failed_codes > self.universe_count
            or self.state not in ("qualified", "historical_data_insufficient")
            or (self.manifest_hash and _HASH.fullmatch(self.manifest_hash) is None)
            or (self.state == "qualified") == bool(reasons)
        ):
            raise ValueError("daily archive qualification is invalid")
        if self.state == "qualified" and (
            self.sessions != 2000
            or self.universe_count == 0
            or self.completed_codes != self.universe_count
            or self.failed_codes != 0
            or self.coverage_status != "coverage_ready"
            or not self.manifest_hash
        ):
            raise ValueError("qualified daily archive does not meet the full archive gate")
        object.__setattr__(self, "failure_reasons", reasons)


@dataclass(frozen=True)
class HistoricalIndustryQualification:
    source: str
    sampled_codes: int
    required_sample_codes: int
    code_available: bool
    industry_available: bool
    classification_available: bool
    effective_from_available: bool
    effective_to_available: bool
    queried_at_available: bool
    source_identity_available: bool
    source_evidence_hash: str
    state: QualificationState
    failure_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        reasons = _reasons(self.failure_reasons)
        if (
            _IDENTITY.fullmatch(self.source) is None
            or self.sampled_codes < 0
            or self.required_sample_codes < 300
            or (self.source_evidence_hash and _HASH.fullmatch(self.source_evidence_hash) is None)
            or self.state not in ("qualified", "historical_data_insufficient")
            or (self.state == "qualified") == bool(reasons)
        ):
            raise ValueError("historical industry qualification is invalid")
        capabilities = (
            self.code_available,
            self.industry_available,
            self.classification_available,
            self.effective_from_available,
            self.effective_to_available,
            self.queried_at_available,
            self.source_identity_available,
        )
        if self.state == "qualified" and (
            self.sampled_codes < self.required_sample_codes or not all(capabilities) or not self.source_evidence_hash
        ):
            raise ValueError("qualified historical industry evidence does not meet the source gate")
        object.__setattr__(self, "failure_reasons", reasons)


@dataclass(frozen=True)
class HistoricalMinuteQualification:
    source: str
    earliest_available: date | None
    sampled_codes: int
    matched_codes: int
    sampled_trade_dates: int
    matched_trade_dates: int
    coverage_ratio: float
    timezone: str
    supports_1120: bool
    supports_1450: bool
    volume_available: bool
    amount_available: bool
    raw_qfq_pair_available: bool
    corporate_action_semantics_proven: bool
    source_evidence_hash: str
    state: QualificationState
    failure_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        counts = (self.sampled_codes, self.matched_codes, self.sampled_trade_dates, self.matched_trade_dates)
        reasons = _reasons(self.failure_reasons)
        if (
            _IDENTITY.fullmatch(self.source) is None
            or any(isinstance(value, bool) or value < 0 for value in counts)
            or self.matched_codes > self.sampled_codes
            or self.matched_trade_dates > self.sampled_trade_dates
            or not math.isfinite(self.coverage_ratio)
            or not 0 <= self.coverage_ratio <= 1
            or self.timezone not in ("", "Asia/Shanghai")
            or _HASH.fullmatch(self.source_evidence_hash) is None
            or self.state not in ("qualified", "historical_data_insufficient")
            or (self.state == "qualified") == bool(reasons)
        ):
            raise ValueError("historical minute qualification is invalid")
        capabilities = (
            self.supports_1120,
            self.supports_1450,
            self.volume_available,
            self.amount_available,
            self.raw_qfq_pair_available,
            self.corporate_action_semantics_proven,
        )
        if self.state == "qualified" and (
            self.earliest_available is None
            or self.sampled_codes == 0
            or self.matched_codes != self.sampled_codes
            or self.sampled_trade_dates == 0
            or self.matched_trade_dates != self.sampled_trade_dates
            or self.coverage_ratio < 0.95
            or self.timezone != "Asia/Shanghai"
            or not all(capabilities)
        ):
            raise ValueError("qualified historical minute evidence does not meet the source gate")
        object.__setattr__(self, "failure_reasons", reasons)


@dataclass(frozen=True)
class PointInTimeDataQualificationReport:
    daily_archive: DailyArchiveQualification
    industry_sources: tuple[HistoricalIndustryQualification, ...]
    minute_sources: tuple[HistoricalMinuteQualification, ...]
    state: QualificationState
    failure_reasons: tuple[str, ...]
    point_in_time_parity: bool = False
    terminal_holdout_opened: bool = False
    production_authority: bool = False
    schema_version: str = "point_in_time_data_qualification"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        industries = tuple(sorted(self.industry_sources, key=lambda item: item.source))
        minutes = tuple(sorted(self.minute_sources, key=lambda item: item.source))
        reasons = _reasons(self.failure_reasons)
        if (
            not industries
            or not minutes
            or len({item.source for item in industries}) != len(industries)
            or len({item.source for item in minutes}) != len(minutes)
            or self.state not in ("qualified", "historical_data_insufficient")
            or (self.state == "qualified") == bool(reasons)
            or self.point_in_time_parity != (self.state == "qualified")
            or self.terminal_holdout_opened
            or self.production_authority
            or self.schema_version != "point_in_time_data_qualification"
        ):
            raise ValueError("point-in-time data qualification report is invalid")
        object.__setattr__(self, "industry_sources", industries)
        object.__setattr__(self, "minute_sources", minutes)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "content_hash", canonical_artifact_hash(self))


def build_point_in_time_data_qualification(
    daily: DailyArchiveQualification,
    industries: tuple[HistoricalIndustryQualification, ...],
    minutes: tuple[HistoricalMinuteQualification, ...],
) -> PointInTimeDataQualificationReport:
    reasons: list[str] = []
    if daily.state != "qualified":
        reasons.append("daily_archive_not_qualified")
    if not any(item.state == "qualified" for item in industries):
        reasons.append("historical_industry_not_qualified")
    if not any(item.state == "qualified" for item in minutes):
        reasons.append("historical_minute_not_qualified")
    return PointInTimeDataQualificationReport(
        daily,
        industries,
        minutes,
        "qualified" if not reasons else "historical_data_insufficient",
        tuple(reasons),
        point_in_time_parity=not reasons,
    )


def _reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    reasons = tuple(sorted(set(values)))
    if any(_IDENTITY.fullmatch(value) is None for value in reasons):
        raise ValueError("qualification failure reason is invalid")
    return reasons


__all__ = [
    "DailyArchiveQualification",
    "HistoricalIndustryQualification",
    "HistoricalMinuteQualification",
    "PointInTimeDataQualificationReport",
    "QualificationState",
    "build_point_in_time_data_qualification",
]
