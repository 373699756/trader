"""Atomic in-memory index for immutable V2 market-data epochs."""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, TypeVar

from trader.application.ports.market import (
    DataPlaneChannel,
    DataPlaneCoverage,
    DataPlaneFailure,
    MarketDataPlaneSnapshot,
)
from trader.domain.market.epochs import CandidateQuoteEpoch, DailyFeaturePack, MarketEpoch, ResearchEpoch
from trader.domain.market.models import MarketQuote

_SHANGHAI_TIMEZONE = "Asia/Shanghai"
_MINIMUM_CANDIDATE_HISTORY_COVERAGE = 0.99


class _EpochValue(Protocol):
    @property
    def trade_date(self) -> date: ...

    @property
    def sequence(self) -> int: ...

    @property
    def version(self) -> str: ...


_EpochT = TypeVar("_EpochT", bound=_EpochValue)


@dataclass(frozen=True)
class EpochPublishResult:
    accepted: bool
    reason: str
    previous_version: str | None
    current_version: str | None


class RealtimeDataPlane:
    """Owns current epoch pointers without performing external I/O."""

    def __init__(self, *, retained_epochs_per_channel: int) -> None:
        if retained_epochs_per_channel <= 0:
            raise ValueError("retained_epochs_per_channel must be positive")
        self._lock = threading.RLock()
        self._daily_current: DailyFeaturePack | None = None
        self._market_current: MarketEpoch | None = None
        self._market_daily: DailyFeaturePack | None = None
        self._candidate_current: CandidateQuoteEpoch | None = None
        self._research_current: ResearchEpoch | None = None
        self._daily_history: deque[DailyFeaturePack] = deque(maxlen=retained_epochs_per_channel)
        self._market_history: deque[MarketEpoch] = deque(maxlen=retained_epochs_per_channel)
        self._candidate_history: deque[CandidateQuoteEpoch] = deque(maxlen=retained_epochs_per_channel)
        self._research_history: deque[ResearchEpoch] = deque(maxlen=retained_epochs_per_channel)
        self._failures: dict[DataPlaneChannel, DataPlaneFailure] = {}

    def publish_daily_features(self, epoch: DailyFeaturePack) -> EpochPublishResult:
        with self._lock:
            rejection = _rejection_reason(self._daily_current, epoch)
            if rejection is not None:
                return _rejected(self._daily_current, rejection)
            previous = self._daily_current
            self._daily_current = epoch
            self._daily_history.append(epoch)
            self._failures.pop(DataPlaneChannel.DAILY_FEATURES, None)
            return _accepted(previous, epoch)

    def publish_market(self, epoch: MarketEpoch) -> EpochPublishResult:
        with self._lock:
            if self._daily_current is None or epoch.daily_feature_pack_version != self._daily_current.version:
                return _rejected(self._market_current, "daily_feature_pack_not_current")
            if epoch.config_version != self._daily_current.config_version:
                return _rejected(self._market_current, "config_version_mismatch")
            rejection = _rejection_reason(self._market_current, epoch)
            if rejection is not None:
                return _rejected(self._market_current, rejection)
            coverage = _coverage(self._daily_current, epoch, None)
            if coverage.security_master_covered_count != coverage.potential_executable_count:
                return _rejected(self._market_current, "security_master_coverage_incomplete")
            previous = self._market_current
            self._market_current = epoch
            self._market_daily = self._daily_current
            self._candidate_current = None
            self._market_history.append(epoch)
            self._failures.pop(DataPlaneChannel.MARKET, None)
            return _accepted(previous, epoch)

    def publish_candidate_quotes(self, epoch: CandidateQuoteEpoch) -> EpochPublishResult:
        with self._lock:
            rejection = _candidate_rejection_reason(
                current=self._candidate_current,
                market=self._market_current,
                daily=self._market_daily,
                incoming=epoch,
            )
            if rejection is not None:
                return _rejected(self._candidate_current, rejection)
            previous = self._candidate_current
            self._candidate_current = epoch
            self._candidate_history.append(epoch)
            self._failures.pop(DataPlaneChannel.CANDIDATE_QUOTES, None)
            return _accepted(previous, epoch)

    def publish_research(self, epoch: ResearchEpoch) -> EpochPublishResult:
        with self._lock:
            rejection = _rejection_reason(self._research_current, epoch)
            if rejection is not None:
                return _rejected(self._research_current, rejection)
            previous = self._research_current
            self._research_current = epoch
            self._research_history.append(epoch)
            self._failures.pop(DataPlaneChannel.RESEARCH, None)
            return _accepted(previous, epoch)

    def record_failure(
        self,
        channel: DataPlaneChannel,
        *,
        reason: str,
        observed_at: datetime,
    ) -> bool:
        _require_shanghai_time(observed_at)
        failure = DataPlaneFailure(reason=reason, observed_at=observed_at)
        with self._lock:
            previous = self._failures.get(channel)
            if previous is not None and previous.observed_at > observed_at:
                return False
            self._failures[channel] = failure
            return True

    def snapshot(self) -> MarketDataPlaneSnapshot:
        with self._lock:
            daily = self._market_daily if self._market_current is not None else self._daily_current
            research = self._research_current
            if daily is not None and research is not None:
                if research.trade_date != daily.trade_date or research.config_version != daily.config_version:
                    research = None
            return MarketDataPlaneSnapshot(
                daily_features=daily,
                market=self._market_current,
                candidate_quotes=self._candidate_current,
                research=research,
                coverage=_coverage(daily, self._market_current, self._candidate_current),
                failures=self._failures,
            )

    def retained_versions(self, channel: DataPlaneChannel) -> tuple[str, ...]:
        with self._lock:
            if channel is DataPlaneChannel.DAILY_FEATURES:
                return tuple(epoch.version for epoch in self._daily_history)
            if channel is DataPlaneChannel.MARKET:
                return tuple(epoch.version for epoch in self._market_history)
            if channel is DataPlaneChannel.CANDIDATE_QUOTES:
                return tuple(epoch.version for epoch in self._candidate_history)
            return tuple(epoch.version for epoch in self._research_history)


def _rejection_reason(current: _EpochT | None, incoming: _EpochT) -> str | None:
    if current is None:
        return None
    current_order = (current.trade_date, current.sequence)
    incoming_order = (incoming.trade_date, incoming.sequence)
    if incoming_order < current_order:
        return "stale_epoch"
    if incoming_order == current_order:
        return "duplicate_epoch" if incoming.version == current.version else "sequence_conflict"
    return None


def _candidate_rejection_reason(
    *,
    current: CandidateQuoteEpoch | None,
    market: MarketEpoch | None,
    daily: DailyFeaturePack | None,
    incoming: CandidateQuoteEpoch,
) -> str | None:
    reason: str | None = None
    if market is None or incoming.market_epoch_version != market.version:
        reason = "market_epoch_not_current"
    elif incoming.config_version != market.config_version:
        reason = "config_version_mismatch"
    elif (epoch_reason := _rejection_reason(current, incoming)) is not None:
        reason = epoch_reason
    elif incoming.requested_codes and not incoming.quotes:
        reason = "invalid_empty_epoch"
    elif any(code not in {quote.code for quote in market.quotes} for code in incoming.requested_codes):
        reason = "candidate_code_not_in_market"
    elif daily is None:
        reason = "daily_feature_pack_not_current"
    else:
        history_ratio = _coverage(daily, market, incoming).candidate_core_history_ratio
        if history_ratio is not None and history_ratio < _MINIMUM_CANDIDATE_HISTORY_COVERAGE:
            reason = "candidate_history_coverage_insufficient"
    return reason


def _accepted(previous: _EpochValue | None, current: _EpochValue) -> EpochPublishResult:
    return EpochPublishResult(
        accepted=True,
        reason="accepted",
        previous_version=previous.version if previous is not None else None,
        current_version=current.version,
    )


def _rejected(current: _EpochValue | None, reason: str) -> EpochPublishResult:
    return EpochPublishResult(
        accepted=False,
        reason=reason,
        previous_version=current.version if current is not None else None,
        current_version=current.version if current is not None else None,
    )


def _require_shanghai_time(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("data-plane failure time must be timezone-aware")
    if getattr(value.tzinfo, "key", None) != _SHANGHAI_TIMEZONE:
        raise ValueError("data-plane failure time must use Asia/Shanghai")


def _coverage(
    daily: DailyFeaturePack | None,
    market: MarketEpoch | None,
    candidate: CandidateQuoteEpoch | None,
) -> DataPlaneCoverage:
    if daily is None or market is None:
        return DataPlaneCoverage()
    rows = {row.code: row for row in daily.rows}
    executable_codes = tuple(quote.code for quote in market.quotes if _is_potentially_executable(quote))
    master_covered = sum(1 for code in executable_codes if code in rows and rows[code].has_security_master)
    candidate_codes = candidate.requested_codes if candidate is not None else ()
    history_covered = sum(1 for code in candidate_codes if code in rows and rows[code].has_core_history)
    return DataPlaneCoverage(
        potential_executable_count=len(executable_codes),
        security_master_covered_count=master_covered,
        candidate_count=len(candidate_codes),
        candidate_core_history_covered_count=history_covered,
    )


def _is_potentially_executable(quote: MarketQuote) -> bool:
    board = quote.board
    price = quote.price
    amount = quote.amount
    return (
        getattr(board, "value", board) in {"main", "chinext", "star"}
        and isinstance(price, (int, float))
        and not isinstance(price, bool)
        and math.isfinite(float(price))
        and float(price) > 0.0
        and isinstance(amount, (int, float))
        and not isinstance(amount, bool)
        and math.isfinite(float(amount))
        and float(amount) > 0.0
        and not quote.is_st
        and not quote.is_suspended
    )


__all__ = [
    "DataPlaneChannel",
    "EpochPublishResult",
    "RealtimeDataPlane",
]
