"""Typed qualification and deterministic merging for historical industry facts."""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from trader.domain.research.baostock_daily import BaoStockBoard
from trader.domain.research.h1_point_in_time import canonical_hash

HistoricalIndustryStatus = Literal["qualified", "historical_data_insufficient"]
HistoricalIndustryCohort = Literal["old", "new", "delisted"]
HistoricalIndustryResolutionStatus = Literal["resolved", "missing", "conflict"]

_CODE = re.compile(r"^[0-9]{6}$")
_IDENTITY = re.compile(r"^[a-z0-9_]{1,64}$")
_REASON = re.compile(r"^[a-z0-9_]{1,96}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class HistoricalIndustrySourceContract:
    source: str
    source_version: str
    code_available: bool
    industry_available: bool
    classification_available: bool
    effective_from_available: bool
    effective_to_available: bool
    queried_at_available: bool
    source_identity_available: bool
    unavailable_reason: str = ""
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        capabilities = self.capabilities
        if (
            _IDENTITY.fullmatch(self.source) is None
            or not self.source_version.strip()
            or any(not isinstance(value, bool) for value in capabilities)
            or (self.unavailable_reason and _REASON.fullmatch(self.unavailable_reason) is None)
        ):
            raise ValueError("historical industry source contract is invalid")
        object.__setattr__(self, "source_version", self.source_version.strip())
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def capabilities(self) -> tuple[bool, ...]:
        return (
            self.code_available,
            self.industry_available,
            self.classification_available,
            self.effective_from_available,
            self.effective_to_available,
            self.queried_at_available,
            self.source_identity_available,
        )


@dataclass(frozen=True)
class HistoricalIndustryFact:
    source: str
    source_version: str
    code: str
    industry: str
    classification: str
    effective_from: date
    effective_to: date | None
    queried_at: datetime | None
    evidence_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        queried_at = self.queried_at
        if (
            _IDENTITY.fullmatch(self.source) is None
            or not self.source_version.strip()
            or _CODE.fullmatch(self.code) is None
            or not self.industry.strip()
            or not self.classification.strip()
            or (self.effective_to is not None and self.effective_to <= self.effective_from)
            or (
                queried_at is not None
                and (
                    queried_at.tzinfo is None
                    or queried_at.utcoffset() is None
                    or getattr(queried_at.tzinfo, "key", None) != _SHANGHAI.key
                )
            )
            or _SHA256.fullmatch(self.evidence_hash) is None
        ):
            raise ValueError("historical industry fact is invalid")
        object.__setattr__(self, "source_version", self.source_version.strip())
        object.__setattr__(self, "industry", self.industry.strip())
        object.__setattr__(self, "classification", self.classification.strip())
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalIndustryStockWindow:
    code: str
    board: BaoStockBoard
    cohort: HistoricalIndustryCohort
    actual_trade_dates: tuple[date, ...]

    def __post_init__(self) -> None:
        dates = tuple(sorted(set(self.actual_trade_dates)))
        if (
            _CODE.fullmatch(self.code) is None
            or self.board not in ("main", "chinext", "star")
            or self.cohort not in ("old", "new", "delisted")
            or not dates
        ):
            raise ValueError("historical industry stock window is invalid")
        object.__setattr__(self, "actual_trade_dates", dates)


@dataclass(frozen=True)
class HistoricalIndustryResolution:
    status: HistoricalIndustryResolutionStatus
    industry: str = ""
    classification: str = ""
    sources: tuple[str, ...] = ()
    evidence_hashes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MergedHistoricalIndustryFact:
    code: str
    industry: str
    classification: str
    effective_from: date
    effective_to: date | None
    sources: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        sources = tuple(sorted(set(self.sources)))
        evidence = tuple(sorted(set(self.evidence_hashes)))
        if (
            _CODE.fullmatch(self.code) is None
            or not self.industry
            or not self.classification
            or (self.effective_to is not None and self.effective_to <= self.effective_from)
            or not sources
            or not evidence
            or any(_IDENTITY.fullmatch(item) is None for item in sources)
            or any(_SHA256.fullmatch(item) is None for item in evidence)
        ):
            raise ValueError("merged historical industry fact is invalid")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "evidence_hashes", evidence)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class HistoricalIndustryMergeResult:
    facts: tuple[MergedHistoricalIndustryFact, ...]
    conflict_count: int

    def __post_init__(self) -> None:
        facts = tuple(sorted(self.facts, key=lambda item: (item.code, item.effective_from)))
        if self.conflict_count < 0 or len({item.content_hash for item in facts}) != len(facts):
            raise ValueError("historical industry merge result is invalid")
        object.__setattr__(self, "facts", facts)


@dataclass(frozen=True)
class HistoricalIndustryStockAudit:
    code: str
    board: BaoStockBoard
    cohort: HistoricalIndustryCohort
    actual_trading_dates: int
    covered_trading_dates: int
    missing_dates: int
    conflict_dates: int
    time_travel_dates: int
    eligible: bool
    failure_reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        reasons = _reasons(self.failure_reasons)
        counts = (
            self.actual_trading_dates,
            self.covered_trading_dates,
            self.missing_dates,
            self.conflict_dates,
            self.time_travel_dates,
        )
        if (
            _CODE.fullmatch(self.code) is None
            or self.board not in ("main", "chinext", "star")
            or self.cohort not in ("old", "new", "delisted")
            or any(value < 0 for value in counts)
            or self.covered_trading_dates + self.missing_dates + self.conflict_dates != self.actual_trading_dates
            or self.eligible == bool(reasons)
        ):
            raise ValueError("historical industry stock audit is invalid")
        object.__setattr__(self, "failure_reasons", reasons)


@dataclass(frozen=True)
class HistoricalIndustrySourceAudit:
    contract: HistoricalIndustrySourceContract
    required_sample_codes: int
    stocks: tuple[HistoricalIndustryStockAudit, ...]
    fact_hashes: tuple[str, ...]
    actual_trading_dates: int
    covered_trading_dates: int
    missing_dates: int
    conflict_dates: int
    time_travel_dates: int
    coverage_ratio: float
    board_counts: tuple[tuple[str, int], ...]
    cohort_counts: tuple[tuple[str, int], ...]
    complete_codes: int
    incomplete_codes: int
    status: HistoricalIndustryStatus
    failure_reasons: tuple[str, ...]
    dataset_hash: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        stocks = tuple(sorted(self.stocks, key=lambda item: item.code))
        fact_hashes = tuple(sorted(set(self.fact_hashes)))
        reasons = _reasons(self.failure_reasons)
        if (
            self.required_sample_codes < 300
            or len({item.code for item in stocks}) != len(stocks)
            or any(_SHA256.fullmatch(item) is None for item in fact_hashes)
            or self.actual_trading_dates != sum(item.actual_trading_dates for item in stocks)
            or self.covered_trading_dates != sum(item.covered_trading_dates for item in stocks)
            or self.missing_dates != sum(item.missing_dates for item in stocks)
            or self.conflict_dates != sum(item.conflict_dates for item in stocks)
            or self.time_travel_dates != sum(item.time_travel_dates for item in stocks)
            or not math.isfinite(self.coverage_ratio)
            or not 0 <= self.coverage_ratio <= 1
            or self.complete_codes + self.incomplete_codes != len(stocks)
            or self.status not in ("qualified", "historical_data_insufficient")
            or (self.status == "qualified") == bool(reasons)
        ):
            raise ValueError("historical industry source audit is invalid")
        object.__setattr__(self, "stocks", stocks)
        object.__setattr__(self, "fact_hashes", fact_hashes)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "dataset_hash", canonical_hash((self.contract.content_hash, fact_hashes)))
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def source(self) -> str:
        return self.contract.source

    @property
    def source_version(self) -> str:
        return self.contract.source_version

    @property
    def sampled_codes(self) -> int:
        return len(self.stocks)

    @property
    def eligible_codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.stocks if item.eligible)


@dataclass(frozen=True)
class HistoricalIndustryDatasetReport:
    daily_manifest_hash: str
    sources: tuple[HistoricalIndustrySourceAudit, ...]
    merged_fact_hashes: tuple[str, ...]
    cross_source_conflicts: int
    status: HistoricalIndustryStatus
    failure_reasons: tuple[str, ...]
    training_authority: bool
    production_authority: bool = False
    schema_version: str = "historical_industry_dataset"
    dataset_hash: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        sources = tuple(sorted(self.sources, key=lambda item: item.source))
        merged_fact_hashes = tuple(sorted(set(self.merged_fact_hashes)))
        reasons = _reasons(self.failure_reasons)
        if (
            _SHA256.fullmatch(self.daily_manifest_hash) is None
            or not sources
            or len({item.source for item in sources}) != len(sources)
            or any(_SHA256.fullmatch(item) is None for item in merged_fact_hashes)
            or self.cross_source_conflicts < 0
            or self.status not in ("qualified", "historical_data_insufficient")
            or (self.status == "qualified") == bool(reasons)
            or self.training_authority != (self.status == "qualified")
            or self.production_authority
            or self.schema_version != "historical_industry_dataset"
        ):
            raise ValueError("historical industry dataset report is invalid")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "merged_fact_hashes", merged_fact_hashes)
        object.__setattr__(self, "failure_reasons", reasons)
        object.__setattr__(self, "dataset_hash", canonical_hash(tuple(item.dataset_hash for item in sources)))
        object.__setattr__(self, "content_hash", canonical_hash(self))

    @property
    def eligible_codes(self) -> tuple[str, ...]:
        if self.status != "qualified":
            return ()
        return tuple(
            sorted({code for source in self.sources if source.status == "qualified" for code in source.eligible_codes})
        )


def resolve_historical_industry(
    facts: tuple[HistoricalIndustryFact, ...],
    code: str,
    trade_date: date,
) -> HistoricalIndustryResolution:
    active = tuple(
        item
        for item in facts
        if item.code == code
        and item.effective_from <= trade_date
        and (item.effective_to is None or trade_date < item.effective_to)
    )
    if not active:
        return HistoricalIndustryResolution("missing")
    values = {(item.industry, item.classification) for item in active}
    if len(values) != 1:
        return HistoricalIndustryResolution(
            "conflict",
            sources=tuple(sorted({item.source for item in active})),
            evidence_hashes=tuple(sorted({item.evidence_hash for item in active})),
        )
    industry, classification = next(iter(values))
    return HistoricalIndustryResolution(
        "resolved",
        industry,
        classification,
        tuple(sorted({item.source for item in active})),
        tuple(sorted({item.evidence_hash for item in active})),
    )


def merge_historical_industry_facts(
    facts: tuple[HistoricalIndustryFact, ...],
) -> HistoricalIndustryMergeResult:
    by_code: dict[str, list[HistoricalIndustryFact]] = defaultdict(list)
    for fact in facts:
        by_code[fact.code].append(fact)
    merged: list[MergedHistoricalIndustryFact] = []
    conflicts = 0
    for code, values in sorted(by_code.items()):
        boundaries = sorted(
            {item.effective_from for item in values}
            | {item.effective_to for item in values if item.effective_to is not None}
        )
        for index, start in enumerate(boundaries):
            end = boundaries[index + 1] if index + 1 < len(boundaries) else None
            resolution = resolve_historical_industry(tuple(values), code, start)
            if resolution.status == "conflict":
                conflicts += 1
            elif resolution.status == "resolved":
                merged.append(
                    MergedHistoricalIndustryFact(
                        code,
                        resolution.industry,
                        resolution.classification,
                        start,
                        end,
                        resolution.sources,
                        resolution.evidence_hashes,
                    )
                )
    return HistoricalIndustryMergeResult(tuple(merged), conflicts)


def build_historical_industry_source_audit(
    contract: HistoricalIndustrySourceContract,
    facts: tuple[HistoricalIndustryFact, ...],
    windows: tuple[HistoricalIndustryStockWindow, ...],
    *,
    required_sample_codes: int = 300,
) -> HistoricalIndustrySourceAudit:
    if any(item.source != contract.source or item.source_version != contract.source_version for item in facts):
        raise ValueError("historical industry facts do not match the source contract")
    by_code: dict[str, list[HistoricalIndustryFact]] = defaultdict(list)
    for fact in facts:
        by_code[fact.code].append(fact)
    stocks = tuple(_audit_stock(contract, tuple(by_code[window.code]), window) for window in windows)
    actual = sum(item.actual_trading_dates for item in stocks)
    covered = sum(item.covered_trading_dates for item in stocks)
    missing = sum(item.missing_dates for item in stocks)
    conflicts = sum(item.conflict_dates for item in stocks)
    time_travel = sum(item.time_travel_dates for item in stocks)
    eligible = sum(item.eligible for item in stocks)
    reasons = _source_failure_reasons(
        contract,
        len(stocks),
        eligible,
        conflicts,
        time_travel,
        bool(facts),
        all(item.queried_at is not None for item in facts),
        required_sample_codes,
    )
    complete = sum(item.missing_dates == 0 and item.conflict_dates == 0 for item in stocks)
    return HistoricalIndustrySourceAudit(
        contract,
        required_sample_codes,
        stocks,
        tuple(item.content_hash for item in facts),
        actual,
        covered,
        missing,
        conflicts,
        time_travel,
        covered / actual if actual else 0.0,
        _dimension_counts(item.board for item in stocks),
        _dimension_counts(item.cohort for item in stocks),
        complete,
        len(stocks) - complete,
        "qualified" if not reasons else "historical_data_insufficient",
        reasons,
    )


def build_historical_industry_dataset_report(
    daily_manifest_hash: str,
    sources: tuple[HistoricalIndustrySourceAudit, ...],
    merge_result: HistoricalIndustryMergeResult,
) -> HistoricalIndustryDatasetReport:
    source_qualified = any(item.status == "qualified" for item in sources)
    qualified = source_qualified and bool(merge_result.facts) and merge_result.conflict_count == 0
    reasons: list[str] = []
    if not source_qualified:
        reasons.append("historical_industry_source_not_qualified")
    if not merge_result.facts:
        reasons.append("historical_industry_merged_fact_evidence_missing")
    if merge_result.conflict_count:
        reasons.append("historical_industry_cross_source_conflict")
    return HistoricalIndustryDatasetReport(
        daily_manifest_hash,
        sources,
        tuple(item.content_hash for item in merge_result.facts),
        merge_result.conflict_count,
        "qualified" if qualified else "historical_data_insufficient",
        tuple(reasons),
        training_authority=qualified,
    )


def _audit_stock(
    contract: HistoricalIndustrySourceContract,
    facts: tuple[HistoricalIndustryFact, ...],
    window: HistoricalIndustryStockWindow,
) -> HistoricalIndustryStockAudit:
    covered = missing = conflicts = time_travel = 0
    for day in window.actual_trade_dates:
        resolution = resolve_historical_industry(facts, window.code, day)
        if resolution.status == "missing":
            missing += 1
        elif resolution.status == "conflict":
            conflicts += 1
        else:
            covered += 1
    reasons: list[str] = []
    if missing:
        reasons.append("industry_dates_missing")
    if conflicts:
        reasons.append("industry_dates_conflict")
    if time_travel:
        reasons.append("industry_time_travel_detected")
    reasons.extend(_capability_reasons(contract))
    if contract.queried_at_available and any(item.queried_at is None for item in facts):
        reasons.append("industry_query_time_unavailable")
    return HistoricalIndustryStockAudit(
        window.code,
        window.board,
        window.cohort,
        len(window.actual_trade_dates),
        covered,
        missing,
        conflicts,
        time_travel,
        not reasons,
        tuple(reasons),
    )


def _source_failure_reasons(
    contract: HistoricalIndustrySourceContract,
    sampled: int,
    eligible: int,
    conflicts: int,
    time_travel: int,
    facts_present: bool,
    fact_query_times_complete: bool,
    required: int,
) -> tuple[str, ...]:
    reasons = list(_capability_reasons(contract))
    if contract.unavailable_reason:
        reasons.append(contract.unavailable_reason)
    if sampled < required:
        reasons.append("industry_sample_below_300")
    if eligible < required:
        reasons.append("industry_eligible_codes_below_300")
    if not facts_present:
        reasons.append("industry_fact_evidence_missing")
    elif contract.queried_at_available and not fact_query_times_complete:
        reasons.append("industry_query_time_unavailable")
    if conflicts:
        reasons.append("industry_conflicts_present")
    if time_travel:
        reasons.append("industry_time_travel_present")
    return _reasons(tuple(reasons))


def _capability_reasons(contract: HistoricalIndustrySourceContract) -> tuple[str, ...]:
    return tuple(
        reason
        for available, reason in zip(
            contract.capabilities,
            (
                "industry_code_unavailable",
                "industry_name_unavailable",
                "industry_classification_unavailable",
                "industry_effective_from_unavailable",
                "industry_effective_to_unavailable",
                "industry_query_time_unavailable",
                "industry_source_identity_unavailable",
            ),
            strict=True,
        )
        if not available
    )


def _dimension_counts(values: Iterable[str]) -> tuple[tuple[str, int], ...]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[str(value)] += 1
    return tuple(sorted(counts.items()))


def _reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    reasons = tuple(sorted(set(values)))
    if any(_REASON.fullmatch(item) is None for item in reasons):
        raise ValueError("historical industry failure reason is invalid")
    return reasons


__all__ = [
    "HistoricalIndustryCohort",
    "HistoricalIndustryDatasetReport",
    "HistoricalIndustryFact",
    "HistoricalIndustryMergeResult",
    "HistoricalIndustrySourceAudit",
    "HistoricalIndustrySourceContract",
    "HistoricalIndustryStatus",
    "HistoricalIndustryStockAudit",
    "HistoricalIndustryStockWindow",
    "MergedHistoricalIndustryFact",
    "build_historical_industry_dataset_report",
    "build_historical_industry_source_audit",
    "merge_historical_industry_facts",
    "resolve_historical_industry",
]
