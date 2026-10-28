"""Ephemeral observation drafts kept outside formal freeze publication."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from trader.recommendation.domain.publication.freeze_identity import ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy


@dataclass(frozen=True)
class FreezeDraftPublishResult:
    accepted: bool
    reason: str


class UnifiedFreezeDraftIndex:
    """Keep the newest incomplete scored freeze per strategy in memory only."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._drafts: dict[Strategy, ScoredFreeze] = {}

    def publish(self, freeze: ScoredFreeze) -> FreezeDraftPublishResult:
        with self._lock:
            current = self._drafts.get(freeze.strategy)
            if current is not None:
                if freeze.trade_date < current.trade_date:
                    return FreezeDraftPublishResult(False, "stale_trade_date")
                if freeze.trade_date == current.trade_date:
                    if freeze.sequence < current.sequence:
                        return FreezeDraftPublishResult(False, "stale_sequence")
                    if freeze.sequence == current.sequence:
                        if freeze == current:
                            return FreezeDraftPublishResult(True, "already_current")
                        return FreezeDraftPublishResult(False, "conflicting_sequence")
            self._drafts[freeze.strategy] = freeze
            return FreezeDraftPublishResult(True, "accepted")

    def snapshot(self, strategy: Strategy) -> ScoredFreeze | None:
        with self._lock:
            return self._drafts.get(strategy)


__all__ = ["FreezeDraftPublishResult", "UnifiedFreezeDraftIndex"]
