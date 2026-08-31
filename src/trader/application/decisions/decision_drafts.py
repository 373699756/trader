"""Ephemeral observation drafts kept outside formal decision publication."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from trader.domain.recommendation.decision_identity import ScoredDecision
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class DecisionDraftPublishResult:
    accepted: bool
    reason: str


class UnifiedDecisionDraftIndex:
    """Keep the newest incomplete scored decision per strategy in memory only."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._drafts: dict[Strategy, ScoredDecision] = {}

    def publish(self, decision: ScoredDecision) -> DecisionDraftPublishResult:
        with self._lock:
            current = self._drafts.get(decision.strategy)
            if current is not None:
                if decision.trade_date < current.trade_date:
                    return DecisionDraftPublishResult(False, "stale_trade_date")
                if decision.trade_date == current.trade_date:
                    if decision.sequence < current.sequence:
                        return DecisionDraftPublishResult(False, "stale_sequence")
                    if decision.sequence == current.sequence:
                        if decision.version == current.version:
                            return DecisionDraftPublishResult(True, "already_current")
                        return DecisionDraftPublishResult(False, "conflicting_sequence")
            self._drafts[decision.strategy] = decision
            return DecisionDraftPublishResult(True, "accepted")

    def snapshot(self, strategy: Strategy) -> ScoredDecision | None:
        with self._lock:
            return self._drafts.get(strategy)


__all__ = ["DecisionDraftPublishResult", "UnifiedDecisionDraftIndex"]
