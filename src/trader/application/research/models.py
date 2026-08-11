"""Immutable application-boundary values for Score-R2 extraction."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TypeVar

from trader.application.ports.types import JsonObject, freeze_json_object
from trader.domain.research.historical import (
    SUPPORTED_RESEARCH_BOARDS,
    CostSettlementBasis,
    HistoricalCandidateSummary,
    ResearchBoard,
    ResearchDataLineage,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_REASON_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_RecordT = TypeVar("_RecordT")


@dataclass(frozen=True)
class HardFilterAggregate:
    board: ResearchBoard
    reason: str
    count: int

    def __post_init__(self) -> None:
        if self.board not in SUPPORTED_RESEARCH_BOARDS or _REASON_CODE.fullmatch(self.reason) is None:
            raise ValueError("hard-filter aggregate identity is invalid")
        if self.count < 1:
            raise ValueError("hard-filter aggregate count must be positive")


@dataclass(frozen=True)
class BoardPointInTimeCoverage:
    board: ResearchBoard
    hard_filter_complete: bool
    point_in_time_complete: bool


@dataclass(frozen=True)
class HistoricalDaySummary:
    trade_date: date
    observed_at: datetime
    daily_feature_pack_version: str
    market_epoch_version: str
    candidate_quote_epoch_version: str | None
    research_epoch_version: str | None
    input_hash: str
    config_version: str
    calendar_version: str
    rule_versions: tuple[str, ...]
    candidates: tuple[HistoricalCandidateSummary, ...]
    hard_filter_aggregates: tuple[HardFilterAggregate, ...]
    board_coverages: tuple[BoardPointInTimeCoverage, ...]
    source_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _validate_day_identity(self)
        candidates = _normalize_day_candidates(self.candidates, self.observed_at)
        aggregates = _normalize_hard_filter_aggregates(self.hard_filter_aggregates)
        coverages = _normalize_board_coverages(self.board_coverages)
        sources = _normalize_source_versions(self.source_versions)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "hard_filter_aggregates", aggregates)
        object.__setattr__(self, "board_coverages", coverages)
        object.__setattr__(self, "rule_versions", tuple(sorted(set(self.rule_versions))))
        object.__setattr__(self, "source_versions", sources)


@dataclass(frozen=True)
class HistoricalFullCandidate:
    code: str
    board: ResearchBoard
    feature_as_of: datetime
    payload: JsonObject
    lineage: ResearchDataLineage

    def __post_init__(self) -> None:
        _require_code(self.code)
        if self.board not in SUPPORTED_RESEARCH_BOARDS:
            raise ValueError("full candidate requires a supported board")
        _require_shanghai(self.feature_as_of, "full candidate feature time")
        if self.lineage.received_at > self.feature_as_of:
            raise ValueError("full candidate lineage cannot be received after feature_as_of")
        object.__setattr__(self, "payload", freeze_json_object(self.payload))


@dataclass(frozen=True)
class HistoricalDailyBar:
    code: str
    session_date: date
    open_price: float
    high: float
    low: float
    close: float
    volume: float
    amount: float
    adjustment_window_id: str
    lineage: ResearchDataLineage

    def __post_init__(self) -> None:
        _validate_bar(self.code, self.adjustment_window_id)
        if self.lineage.source_time.date() < self.session_date:
            raise ValueError("daily bar lineage cannot predate its session")
        _validate_finite((self.open_price, self.high, self.low, self.close, self.volume, self.amount))
        if min(self.open_price, self.high, self.low, self.close) <= 0.0 or min(self.volume, self.amount) < 0.0:
            raise ValueError("historical daily bar prices must be positive and flows non-negative")
        if self.low > min(self.open_price, self.close) or self.high < max(self.open_price, self.close):
            raise ValueError("historical daily bar OHLC is inconsistent")


@dataclass(frozen=True)
class HistoricalMinuteBar:
    code: str
    minute: datetime
    close: float
    volume: float
    amount: float
    lineage: ResearchDataLineage

    def __post_init__(self) -> None:
        _validate_bar(self.code)
        _require_shanghai(self.minute, "historical minute")
        _validate_finite((self.close, self.volume, self.amount))
        if self.close <= 0.0 or min(self.volume, self.amount) < 0.0:
            raise ValueError("historical minute bar values are invalid")


@dataclass(frozen=True)
class AdjustmentFactorWindow:
    window_id: str
    code: str
    as_of: datetime
    factors: tuple[tuple[date, float], ...]
    lineage: ResearchDataLineage

    def __post_init__(self) -> None:
        if not self.window_id:
            raise ValueError("adjustment window identity must not be empty")
        _require_code(self.code)
        _require_shanghai(self.as_of, "adjustment window time")
        if self.lineage.received_at > self.as_of:
            raise ValueError("adjustment window lineage cannot be received after as_of")
        factors = tuple(sorted(self.factors))
        if not factors or len({day for day, _value in factors}) != len(factors):
            raise ValueError("adjustment factors must contain unique dates")
        if any(not math.isfinite(value) or value <= 0.0 for _day, value in factors):
            raise ValueError("adjustment factors must be finite and positive")
        if any(day > self.as_of.date() for day, _value in factors):
            raise ValueError("adjustment factors cannot be from the future")
        object.__setattr__(self, "factors", factors)


@dataclass(frozen=True)
class HistoricalSettlementEvidence:
    basis: CostSettlementBasis
    lineage: ResearchDataLineage

    def __post_init__(self) -> None:
        if self.lineage.source_time.date() < self.basis.label_date:
            raise ValueError("settlement evidence cannot predate its label")


@dataclass(frozen=True)
class HistoricalFullFieldBundle:
    trade_date: date
    input_hash: str
    requested_codes: tuple[str, ...]
    candidates: tuple[HistoricalFullCandidate, ...]
    daily_bars: tuple[HistoricalDailyBar, ...]
    minute_bars: tuple[HistoricalMinuteBar, ...]
    adjustment_windows: tuple[AdjustmentFactorWindow, ...]
    settlements: tuple[HistoricalSettlementEvidence, ...]
    settlement_complete_boards: tuple[ResearchBoard, ...]

    def __post_init__(self) -> None:
        requested = _validate_full_field_identity(self.input_hash, self.requested_codes)
        candidates = tuple(sorted(self.candidates, key=lambda item: item.code))
        if {item.code for item in candidates} != set(requested) or len(candidates) != len(requested):
            raise ValueError("full-field candidates must exactly match requested codes")
        daily = _deduplicate(self.daily_bars, lambda item: (item.code, item.session_date), "daily bars")
        minute = _deduplicate(self.minute_bars, lambda item: (item.code, item.minute), "minute bars")
        windows = _deduplicate(self.adjustment_windows, lambda item: item.window_id, "adjustment windows")
        settlements = _deduplicate(self.settlements, lambda item: item.basis.code, "settlements")
        if {item.basis.code for item in settlements} != set(requested):
            raise ValueError("settlements must exactly cover requested codes")
        if any(item.basis.decision_date != self.trade_date for item in settlements):
            raise ValueError("settlement decision dates must match the full-field bundle")
        board_by_code = {item.code: item.board for item in candidates}
        if any(board_by_code[item.basis.code] != item.basis.board for item in settlements):
            raise ValueError("settlement boards must match full-field candidates")
        related_codes = {item.code for group in (daily, minute, windows) for item in group}
        if not related_codes.issubset(set(requested)):
            raise ValueError("full-field records must belong to requested codes")
        if set(self.settlement_complete_boards) != set(SUPPORTED_RESEARCH_BOARDS) or len(
            self.settlement_complete_boards
        ) != len(SUPPORTED_RESEARCH_BOARDS):
            raise ValueError("settlement coverage must be complete for all three boards")
        window_by_id = {item.window_id: item for item in windows}
        if any(
            item.adjustment_window_id not in window_by_id or window_by_id[item.adjustment_window_id].code != item.code
            for item in daily
        ):
            raise ValueError("daily bars must reference a matching shared adjustment window")
        if requested and (
            {item.code for item in daily} != set(requested)
            or {item.code for item in minute} != set(requested)
            or {item.code for item in windows} != set(requested)
            or len(windows) != len(requested)
        ):
            raise ValueError("full fields must include bars and adjustment windows for every requested code")
        _validate_full_field_cutoffs(candidates, daily, minute, windows, self.trade_date)
        object.__setattr__(self, "requested_codes", requested)
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(self, "daily_bars", daily)
        object.__setattr__(self, "minute_bars", minute)
        object.__setattr__(self, "adjustment_windows", windows)
        object.__setattr__(self, "settlements", settlements)
        object.__setattr__(self, "settlement_complete_boards", tuple(sorted(self.settlement_complete_boards)))


def _validate_full_field_identity(input_hash: str, requested_codes: tuple[str, ...]) -> tuple[str, ...]:
    if _SHA256.fullmatch(input_hash) is None:
        raise ValueError("full-field input identity must be SHA-256")
    requested = tuple(sorted(set(requested_codes)))
    if requested != requested_codes or any(not code for code in requested):
        raise ValueError("full-field request codes must be sorted and unique")
    return requested


def _deduplicate(
    values: tuple[_RecordT, ...],
    key: Callable[[_RecordT], object],
    label: str,
) -> tuple[_RecordT, ...]:
    retained: dict[object, _RecordT] = {}
    for value in values:
        identity = key(value)
        current = retained.get(identity)
        if current is not None and current != value:
            raise ValueError(f"same-key {label} contain conflicting content")
        retained[identity] = value
    return tuple(retained[identity] for identity in sorted(retained, key=repr))


def _validate_full_field_cutoffs(
    candidates: tuple[HistoricalFullCandidate, ...],
    daily: tuple[HistoricalDailyBar, ...],
    minute: tuple[HistoricalMinuteBar, ...],
    windows: tuple[AdjustmentFactorWindow, ...],
    trade_date: date,
) -> None:
    cutoff_by_code = {item.code: item.feature_as_of for item in candidates}
    if any(item.feature_as_of.date() != trade_date for item in candidates):
        raise ValueError("full-field candidate cutoffs must match the trade date")
    if any(item.session_date >= trade_date or item.lineage.received_at > cutoff_by_code[item.code] for item in daily):
        raise ValueError("daily bars must be completed before the trade date and available by the candidate cutoff")
    if any(
        item.minute > cutoff_by_code[item.code] or item.lineage.received_at > cutoff_by_code[item.code]
        for item in minute
    ):
        raise ValueError("minute bars must be available by the candidate cutoff")
    if any(
        item.as_of > cutoff_by_code[item.code] or item.lineage.received_at > cutoff_by_code[item.code]
        for item in windows
    ):
        raise ValueError("adjustment windows must be available by the candidate cutoff")


def _validate_day_identity(summary: HistoricalDaySummary) -> None:
    _require_shanghai(summary.observed_at, "historical day observation")
    if summary.observed_at.date() != summary.trade_date:
        raise ValueError("historical day observation must match trade date")
    if not all(
        (
            summary.daily_feature_pack_version,
            summary.market_epoch_version,
            summary.config_version,
            summary.calendar_version,
        )
    ):
        raise ValueError("historical day identity is invalid")
    for optional_version in (summary.candidate_quote_epoch_version, summary.research_epoch_version):
        if optional_version is not None and not optional_version:
            raise ValueError("optional historical epoch versions cannot be empty")
    if _SHA256.fullmatch(summary.input_hash) is None:
        raise ValueError("historical day identity is invalid")
    if not summary.rule_versions or any(not value for value in summary.rule_versions):
        raise ValueError("historical day rule versions must not be empty")


def _normalize_day_candidates(
    values: tuple[HistoricalCandidateSummary, ...],
    observed_at: datetime,
) -> tuple[HistoricalCandidateSummary, ...]:
    candidates = tuple(sorted(values, key=lambda item: item.code))
    if len({item.code for item in candidates}) != len(candidates):
        raise ValueError("historical day candidates must be unique")
    if any(item.feature_as_of > observed_at or item.lineage.received_at > observed_at for item in candidates):
        raise ValueError("historical day cannot contain future candidate inputs")
    return candidates


def _normalize_hard_filter_aggregates(
    values: tuple[HardFilterAggregate, ...],
) -> tuple[HardFilterAggregate, ...]:
    aggregates = tuple(sorted(values, key=lambda item: (item.board, item.reason)))
    if len({(item.board, item.reason) for item in aggregates}) != len(aggregates):
        raise ValueError("historical hard-filter aggregates must be unique")
    return aggregates


def _normalize_board_coverages(
    values: tuple[BoardPointInTimeCoverage, ...],
) -> tuple[BoardPointInTimeCoverage, ...]:
    coverages = tuple(sorted(values, key=lambda item: item.board))
    if {item.board for item in coverages} != set(SUPPORTED_RESEARCH_BOARDS) or len(coverages) != len(
        SUPPORTED_RESEARCH_BOARDS
    ):
        raise ValueError("historical day requires coverage for all three boards")
    if any(not item.hard_filter_complete or not item.point_in_time_complete for item in coverages):
        raise ValueError("historical day board point-in-time coverage is incomplete")
    return coverages


def _normalize_source_versions(values: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    sources = tuple(sorted(values))
    if not sources or len({name for name, _version in sources}) != len(sources):
        raise ValueError("historical day source versions are invalid")
    if any(not name or not version for name, version in sources):
        raise ValueError("historical day source versions must not be empty")
    return sources


def _validate_bar(
    code: str,
    window_id: str | None = None,
) -> None:
    _require_code(code)
    if window_id is not None and not window_id:
        raise ValueError("historical daily bar adjustment window must not be empty")


def _validate_finite(values: tuple[float, ...]) -> None:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("historical numeric values must be finite")


def _require_code(code: str) -> None:
    if len(code) != 6 or not code.isdigit():
        raise ValueError("research stock code must contain exactly six digits")


def _require_shanghai(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None or getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError(f"{name} must use Asia/Shanghai")


__all__ = [
    "AdjustmentFactorWindow",
    "BoardPointInTimeCoverage",
    "HardFilterAggregate",
    "HistoricalDailyBar",
    "HistoricalDaySummary",
    "HistoricalFullCandidate",
    "HistoricalFullFieldBundle",
    "HistoricalMinuteBar",
    "HistoricalSettlementEvidence",
]
