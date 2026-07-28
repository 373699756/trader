"""Tomorrow decision freeze persistence port."""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol

from trader.domain.recommendation.tomorrow_freeze import (
    TomorrowDecisionFreeze,
    TomorrowFreezeCheckpoint,
)


class DecisionFreezeError(RuntimeError):
    """Base failure for the tomorrow v2 freeze repository."""


class DecisionFreezeConflictError(DecisionFreezeError):
    """A different immutable formal record already exists."""


class DecisionFreezeUnavailableError(DecisionFreezeError):
    """Persistence is unavailable or a stored manifest cannot be verified."""


class TomorrowDecisionFreezePort(Protocol):
    def save_checkpoint(self, checkpoint: TomorrowFreezeCheckpoint) -> None: ...

    def load_checkpoint(self, trade_date: date) -> TomorrowFreezeCheckpoint | None: ...

    def consume_checkpoint(
        self,
        checkpoint_version: str,
        *,
        consumed_at: datetime,
    ) -> None: ...

    def commit_freeze(self, frozen: TomorrowDecisionFreeze) -> None: ...

    def load_frozen(self, trade_date: date) -> TomorrowDecisionFreeze | None: ...


__all__ = [
    "DecisionFreezeConflictError",
    "DecisionFreezeError",
    "DecisionFreezeUnavailableError",
    "TomorrowDecisionFreezePort",
]
