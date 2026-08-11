"""Formal V2 scored-decision persistence boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol

from trader.domain.recommendation.decision_identity import CommittedDecisionRecord
from trader.domain.recommendation.models import Strategy


class DecisionRecordError(RuntimeError):
    """Base failure for immutable V2 decision records."""


class DecisionRecordConflictError(DecisionRecordError):
    """A strategy/date key already contains different immutable content."""


class DecisionRecordUnavailableError(DecisionRecordError):
    """A persistence or verification failure prevented a trusted read/write."""


@dataclass(frozen=True)
class DecisionRecordRecoverySummary:
    recovered: int = 0
    quarantined: int = 0
    orphaned: int = 0


class DecisionRecordRepositoryPort(Protocol):
    def initialize(self) -> None: ...

    def commit(self, record: CommittedDecisionRecord) -> None: ...

    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None: ...

    def recover(self) -> DecisionRecordRecoverySummary: ...


__all__ = [
    "DecisionRecordConflictError",
    "DecisionRecordError",
    "DecisionRecordRecoverySummary",
    "DecisionRecordRepositoryPort",
    "DecisionRecordUnavailableError",
]
