"""Immutable dated industry preparation for one historical price window."""

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from trader.download.domain.baostock_daily import BaoStockIndustryInterval, BaoStockSecurity
from trader.download.domain.history_sync import HistoryIndustryReadiness
from trader.download.domain.industry_evidence import OfficialIndustryEvidence
from trader.infra.serialization.canonical_codec import CanonicalDigest

_CLASSIFICATION = "中国上市公司协会上市公司行业分类标准"


@dataclass(frozen=True)
class HistoricalIndustryPreparation:
    intervals: tuple[BaoStockIndustryInterval, ...]
    readiness: HistoryIndustryReadiness
    evidence_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            type(self.intervals) is not tuple
            or any(not isinstance(item, BaoStockIndustryInterval) for item in self.intervals)
            or not isinstance(self.readiness, HistoryIndustryReadiness)
            or type(self.evidence_hashes) is not tuple
            or self.evidence_hashes != tuple(sorted(set(self.evidence_hashes)))
            or any(len(value) != 64 or any(character not in "0123456789abcdef" for character in value) for value in self.evidence_hashes)
        ):
            raise ValueError("historical_industry_preparation_invalid")

    @property
    def data_version(self) -> str:
        return CanonicalDigest((self.intervals, self.evidence_hashes))


def BuildHistoricalIndustryPreparation(
    universe: tuple[BaoStockSecurity, ...],
    calendar_dates: tuple[date, ...],
    observed_at: datetime,
    evidence: tuple[OfficialIndustryEvidence, ...],
    candidate_codes: frozenset[str],
) -> HistoricalIndustryPreparation:
    """Project only verified disclosed facts and count complete trading-date coverage."""
    codes = tuple(item.code for item in universe)
    if (
        not universe
        or len(universe) > 12_000
        or codes != tuple(sorted(set(codes)))
        or not calendar_dates
        or len(calendar_dates) > 20_000
        or any(type(day) is not date for day in calendar_dates)
        or calendar_dates != tuple(sorted(set(calendar_dates)))
        or observed_at.utcoffset() != timedelta(hours=8)
        or calendar_dates[-1] > observed_at.date()
        or len(evidence) > 256_000
        or not candidate_codes.issubset(codes)
    ):
        raise ValueError("historical_industry_window_invalid")
    securities = {item.code: item for item in universe}
    intervals: list[BaoStockIndustryInterval] = []
    evidence_hashes: set[str] = set()
    candidates = set(candidate_codes)
    for item in evidence:
        if (
            item.classification != _CLASSIFICATION
            or item.industry != f"capco:{item.native_category}{item.native_name}"
        ):
            raise ValueError("historical_industry_classification_invalid")
        if item.received_at > observed_at or item.disclosed_on > observed_at.date():
            continue
        security = securities.get(item.code)
        if security is None:
            continue
        start = max(item.effective_from, security.listed_on)
        end = min(item.effective_to, security.delisted_on) if security.delisted_on is not None else item.effective_to
        if start >= end or start > calendar_dates[-1] or end <= calendar_dates[0]:
            continue
        candidates.add(item.code)
        evidence_hashes.add(item.identity)
        intervals.append(BaoStockIndustryInterval(item.code, start, end, item.industry, item.classification))
    merged = _merge_intervals(intervals)
    by_code: dict[str, list[BaoStockIndustryInterval]] = {}
    for interval in merged:
        by_code.setdefault(interval.code, []).append(interval)
    covered = sum(_has_coverage(security, calendar_dates, by_code.get(security.code, [])) for security in universe)
    return HistoricalIndustryPreparation(
        merged,
        HistoryIndustryReadiness(len(universe), len(candidates), covered, source="capco"),
        tuple(sorted(evidence_hashes)),
    )


def _merge_intervals(intervals: list[BaoStockIndustryInterval]) -> tuple[BaoStockIndustryInterval, ...]:
    merged: list[BaoStockIndustryInterval] = []
    for interval in sorted(intervals, key=lambda item: (item.code, item.effective_from)):
        if merged and merged[-1].code == interval.code:
            previous = merged[-1]
            if previous.effective_to is None or interval.effective_to is None:
                raise ValueError("historical_industry_interval_unbounded")
            if interval.effective_from < previous.effective_to and previous.industry != interval.industry:
                raise ValueError("historical_industry_interval_conflict")
            if interval.effective_from <= previous.effective_to and previous.industry == interval.industry:
                merged[-1] = replace(previous, effective_to=max(previous.effective_to, interval.effective_to))
                continue
        merged.append(interval)
    return tuple(merged)


def _has_coverage(
    security: BaoStockSecurity,
    calendar_dates: tuple[date, ...],
    intervals: list[BaoStockIndustryInterval],
) -> bool:
    required = tuple(day for day in calendar_dates if day >= security.listed_on and (
        security.delisted_on is None or day < security.delisted_on
    ))
    if not required:
        return False
    position = 0
    for day in required:
        while position < len(intervals):
            interval = intervals[position]
            if interval.effective_to is None or day < interval.effective_to:
                break
            position += 1
        if position == len(intervals) or day < intervals[position].effective_from:
            return False
    return True


__all__ = [
    "HistoricalIndustryPreparation",
    "BuildHistoricalIndustryPreparation",
]
