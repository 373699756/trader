"""Thread-safe current tomorrow decision index with an atomic freeze seal."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from trader.domain.recommendation.tomorrow_freeze import TomorrowDecisionFreeze
from trader.domain.recommendation.tomorrow_fusion import DecisionEpoch


@dataclass(frozen=True)
class DecisionPublishResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class DecisionSealResult:
    accepted: bool
    reason: str
    decision: DecisionEpoch | None = None
    source: Literal["current", "fallback", "explicit"] | None = None


@dataclass(frozen=True)
class CurrentDecisionSnapshot:
    decision: DecisionEpoch | None
    frozen: TomorrowDecisionFreeze | None


class CurrentDecisionIndex:
    """In-memory CAS index; durable ownership remains in the repository."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: DecisionEpoch | None = None
        self._sealed_decision: DecisionEpoch | None = None
        self._sealed_boundary: datetime | None = None
        self._sealed_source: Literal["current", "fallback", "explicit"] | None = None
        self._frozen: TomorrowDecisionFreeze | None = None

    def publish(
        self,
        decision: DecisionEpoch,
        *,
        expected_current_version: str | None,
    ) -> DecisionPublishResult:
        with self._lock:
            rejection = self._publication_rejection(
                decision,
                expected_current_version,
            )
            if rejection is not None:
                return DecisionPublishResult(False, rejection)
            if self._current is not None and decision.trade_date > self._current.trade_date:
                self._clear_freeze_state()
            self._current = decision
            return DecisionPublishResult(True, "accepted")

    def latest(self) -> DecisionEpoch | None:
        with self._lock:
            return self._current

    def frozen(self) -> TomorrowDecisionFreeze | None:
        with self._lock:
            return self._frozen

    def snapshot(self) -> CurrentDecisionSnapshot:
        with self._lock:
            return CurrentDecisionSnapshot(self._current, self._frozen)

    def is_sealed(self, trade_date: date) -> bool:
        with self._lock:
            return self._sealed_decision is not None and self._sealed_decision.trade_date == trade_date

    def seal_for_freeze(
        self,
        *,
        boundary_at: datetime,
        fallback_decision: DecisionEpoch | None,
    ) -> DecisionSealResult:
        with self._lock:
            if self._sealed_decision is not None:
                if self._sealed_boundary == boundary_at:
                    return DecisionSealResult(
                        True,
                        "already_sealed",
                        self._sealed_decision,
                        self._sealed_source,
                    )
                return DecisionSealResult(False, "different_boundary")
            current = self._current
            if current is not None and current.trade_date == boundary_at.date() and current.observed_at <= boundary_at:
                return self._seal(current, boundary_at, "current")
            if (
                fallback_decision is not None
                and fallback_decision.trade_date == boundary_at.date()
                and fallback_decision.observed_at <= boundary_at
            ):
                return self._seal(fallback_decision, boundary_at, "fallback")
            return DecisionSealResult(False, "no_eligible_decision")

    def seal_close_fallback(
        self,
        decision: DecisionEpoch,
        *,
        boundary_at: datetime,
    ) -> DecisionSealResult:
        with self._lock:
            if self._sealed_decision is not None:
                if self._sealed_decision.version == decision.version:
                    return DecisionSealResult(
                        True,
                        "already_sealed",
                        self._sealed_decision,
                        self._sealed_source,
                    )
                return DecisionSealResult(False, "freeze_sealed")
            return self._seal(decision, boundary_at, "explicit")

    def commit_frozen(self, frozen: TomorrowDecisionFreeze) -> bool:
        with self._lock:
            if self._frozen is not None:
                return self._frozen == frozen
            if self._sealed_decision is None or frozen.decision.version != self._sealed_decision.version:
                return False
            self._current = frozen.decision
            self._frozen = frozen
            return True

    def restore_frozen(self, frozen: TomorrowDecisionFreeze) -> bool:
        with self._lock:
            if self._current is not None and self._current.trade_date > frozen.trade_date:
                return False
            if (
                self._frozen is not None
                and self._frozen.trade_date == frozen.trade_date
                and self._frozen.version != frozen.version
            ):
                return False
            self._current = frozen.decision
            self._sealed_decision = frozen.decision
            self._sealed_boundary = frozen.frozen_at
            self._sealed_source = "fallback" if frozen.freeze_kind == "checkpoint_recovery" else "current"
            self._frozen = frozen
            return True

    def _seal(
        self,
        decision: DecisionEpoch,
        boundary_at: datetime,
        source: Literal["current", "fallback", "explicit"],
    ) -> DecisionSealResult:
        self._sealed_decision = decision
        self._sealed_boundary = boundary_at
        self._sealed_source = source
        return DecisionSealResult(True, "sealed", decision, source)

    def _is_same_day_sealed(self, decision: DecisionEpoch) -> bool:
        return self._sealed_decision is not None and self._sealed_decision.trade_date == decision.trade_date

    def _publication_rejection(
        self,
        decision: DecisionEpoch,
        expected_current_version: str | None,
    ) -> str | None:
        if self._is_same_day_sealed(decision):
            return "freeze_sealed"
        current = self._current
        actual_version = current.version if current is not None else None
        if actual_version != expected_current_version:
            return "cas_mismatch"
        if current is None:
            return None
        if decision.trade_date < current.trade_date:
            return "stale_trade_date"
        if decision.trade_date == current.trade_date:
            return _same_day_rejection(current, decision)
        return None

    def _clear_freeze_state(self) -> None:
        self._sealed_decision = None
        self._sealed_boundary = None
        self._sealed_source = None
        self._frozen = None


def _same_day_rejection(
    current: DecisionEpoch,
    decision: DecisionEpoch,
) -> str | None:
    if decision.sequence < current.sequence:
        return "stale_sequence"
    if decision.sequence == current.sequence and decision.version != current.version:
        return "sequence_conflict"
    if decision.projection_stage == "hybrid" and (
        current.projection_stage != "local" or decision.parent_decision_version != current.version
    ):
        return "parent_mismatch"
    return None


__all__ = [
    "CurrentDecisionSnapshot",
    "CurrentDecisionIndex",
    "DecisionPublishResult",
    "DecisionSealResult",
]
