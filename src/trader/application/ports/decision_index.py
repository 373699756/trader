"""Application port for freeze coordinators to interact with the decision index."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Protocol

from trader.domain.recommendation.decision_identity import (
    CommittedDecisionRecord,
    DecisionIdentity,
    ScoredDecision,
)
from trader.domain.recommendation.models import Strategy


class DecisionSnapshotPort(Protocol):
    @property
    def current(self) -> DecisionIdentity | None: ...


class DecisionSealPort(Protocol):
    @property
    def accepted(self) -> bool: ...

    @property
    def reason(self) -> str: ...

    @property
    def decision(self) -> ScoredDecision | None: ...

    @property
    def source(self) -> Literal["current", "checkpoint", "explicit"] | None: ...


class DecisionIndexPort(Protocol):
    def snapshot(self, strategy: Strategy) -> DecisionSnapshotPort: ...

    def is_sealed(self, strategy: Strategy, trade_date: date) -> bool: ...

    def is_closed(self, strategy: Strategy, trade_date: date) -> bool: ...

    def close_for_date(self, strategy: Strategy, trade_date: date, *, boundary_at: datetime) -> bool: ...

    def discard_closed_current(self, strategy: Strategy, trade_date: date) -> bool: ...

    def seal_for_freeze(
        self,
        strategy: Strategy,
        *,
        boundary_at: datetime,
        fallback_decision: ScoredDecision | None = None,
    ) -> DecisionSealPort: ...

    def seal_close_fallback(
        self,
        decision: ScoredDecision,
        *,
        boundary_at: datetime,
        official_close_version: str,
    ) -> DecisionSealPort: ...

    def commit_formal(self, record: CommittedDecisionRecord) -> bool: ...

    def restore_formal(self, record: CommittedDecisionRecord) -> bool: ...


__all__ = ["DecisionIndexPort", "DecisionSealPort", "DecisionSnapshotPort"]
