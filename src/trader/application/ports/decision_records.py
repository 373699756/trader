"""Formal V2 scored-decision persistence boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from trader.domain.recommendation.decision_identity import CommittedDecisionRecord, ScoredDecision
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


@dataclass(frozen=True)
class V2DecisionCheckpoint:
    decision: ScoredDecision
    boundary_at: datetime
    version: str = field(init=False)

    def __post_init__(self) -> None:
        if self.boundary_at.tzinfo is None or self.boundary_at.utcoffset() is None:
            raise ValueError("decision checkpoint boundary must be timezone-aware")
        if getattr(self.boundary_at.tzinfo, "key", None) != "Asia/Shanghai":
            raise ValueError("decision checkpoint boundary must use Asia/Shanghai")
        if self.decision.trade_date != self.boundary_at.date() or self.decision.observed_at > self.boundary_at:
            raise ValueError("decision checkpoint coordinates are invalid")
        digest = hashlib.sha256(f"{self.decision.version}|{self.boundary_at.isoformat()}".encode()).hexdigest()
        object.__setattr__(
            self,
            "version",
            f"checkpoint:{self.decision.strategy.value}:{self.decision.trade_date.isoformat()}:{digest[:16]}",
        )


class DecisionRecordRepositoryPort(Protocol):
    def initialize(self) -> None: ...

    def commit(self, record: CommittedDecisionRecord) -> None: ...

    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None: ...

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]: ...

    def save_checkpoint(self, checkpoint: V2DecisionCheckpoint) -> None: ...

    def load_checkpoint(self, strategy: Strategy, trade_date: date) -> V2DecisionCheckpoint | None: ...

    def consume_checkpoint(self, checkpoint: V2DecisionCheckpoint, *, consumed_at: datetime) -> None: ...

    def recover(self) -> DecisionRecordRecoverySummary: ...


__all__ = [
    "DecisionRecordConflictError",
    "DecisionRecordError",
    "DecisionRecordRecoverySummary",
    "DecisionRecordRepositoryPort",
    "DecisionRecordUnavailableError",
    "V2DecisionCheckpoint",
]
