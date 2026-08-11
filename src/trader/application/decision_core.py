"""Thread-safe unified V2 current identity and quote-overlay CAS index."""

from __future__ import annotations

import threading
from dataclasses import dataclass

from trader.application.decision_events import V2DecisionCommitted, build_v2_decision_committed
from trader.domain.recommendation.decision_identity import (
    DecisionIdentity,
    DecisionOverlay,
    ScoredDecision,
    identity_codes,
)
from trader.domain.recommendation.models import Strategy


@dataclass(frozen=True)
class UnifiedDecisionPublishResult:
    accepted: bool
    reason: str
    event: V2DecisionCommitted | None = None


@dataclass(frozen=True)
class UnifiedOverlayPublishResult:
    accepted: bool
    reason: str


@dataclass(frozen=True)
class UnifiedDecisionSnapshot:
    current: DecisionIdentity | None
    overlay: DecisionOverlay | None


class UnifiedDecisionIndex:
    """Per-strategy expected-version CAS with no cross-strategy mutable state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._current: dict[Strategy, DecisionIdentity] = {}
        self._overlays: dict[Strategy, DecisionOverlay] = {}

    def publish(
        self,
        identity: DecisionIdentity,
        *,
        expected_version: str | None,
    ) -> UnifiedDecisionPublishResult:
        with self._lock:
            current = self._current.get(identity.strategy)
            rejection = _identity_rejection(current, identity, expected_version)
            if rejection is not None:
                return UnifiedDecisionPublishResult(False, rejection)
            self._current[identity.strategy] = identity
            overlay = self._overlays.get(identity.strategy)
            if overlay is not None and overlay.parent_version != identity.version:
                self._overlays.pop(identity.strategy, None)
            event = build_v2_decision_committed(identity) if isinstance(identity, ScoredDecision) else None
            return UnifiedDecisionPublishResult(True, "accepted", event)

    def publish_overlay(
        self,
        overlay: DecisionOverlay,
        *,
        expected_version: str | None,
    ) -> UnifiedOverlayPublishResult:
        with self._lock:
            current = self._current.get(overlay.strategy)
            existing = self._overlays.get(overlay.strategy)
            rejection = _overlay_rejection(current, existing, overlay, expected_version)
            if rejection is not None:
                return UnifiedOverlayPublishResult(False, rejection)
            self._overlays[overlay.strategy] = overlay
            return UnifiedOverlayPublishResult(True, "accepted")

    def snapshot(self, strategy: Strategy) -> UnifiedDecisionSnapshot:
        with self._lock:
            return UnifiedDecisionSnapshot(self._current.get(strategy), self._overlays.get(strategy))


def _identity_rejection(
    current: DecisionIdentity | None,
    candidate: DecisionIdentity,
    expected_version: str | None,
) -> str | None:
    actual_version = current.version if current is not None else None
    if actual_version != expected_version:
        return "cas_mismatch"
    if current is None:
        return _hybrid_parent_rejection(None, candidate)
    if candidate.trade_date < current.trade_date:
        return "stale_trade_date"
    if candidate.trade_date == current.trade_date:
        if candidate.sequence < current.sequence:
            return "stale_sequence"
        if candidate.sequence == current.sequence and candidate.version != current.version:
            return "sequence_conflict"
    return _hybrid_parent_rejection(current, candidate)


def _hybrid_parent_rejection(
    current: DecisionIdentity | None,
    candidate: DecisionIdentity,
) -> str | None:
    if not isinstance(candidate, ScoredDecision) or candidate.stage != "hybrid":
        return None
    if (
        not isinstance(current, ScoredDecision)
        or current.stage != "local"
        or current.strategy is not candidate.strategy
        or current.trade_date != candidate.trade_date
        or candidate.parent_version != current.version
    ):
        return "parent_mismatch"
    return None


def _overlay_rejection(
    current: DecisionIdentity | None,
    existing: DecisionOverlay | None,
    candidate: DecisionOverlay,
    expected_version: str | None,
) -> str | None:
    rejection: str | None = None
    if current is None or current.version != candidate.parent_version:
        rejection = "parent_mismatch"
    elif current.trade_date != candidate.trade_date:
        rejection = "trade_date_mismatch"
    elif candidate.observed_at < current.observed_at:
        rejection = "stale_overlay"
    elif any(quote.code not in identity_codes(current) for quote in candidate.quotes):
        rejection = "quote_scope_mismatch"
    elif (existing.version if existing is not None else None) != expected_version:
        rejection = "overlay_cas_mismatch"
    elif existing is not None and candidate.observed_at < existing.observed_at:
        rejection = "stale_overlay"
    elif (
        existing is not None and candidate.observed_at == existing.observed_at and candidate.version != existing.version
    ):
        rejection = "overlay_conflict"
    return rejection


__all__ = [
    "UnifiedDecisionIndex",
    "UnifiedDecisionPublishResult",
    "UnifiedDecisionSnapshot",
    "UnifiedOverlayPublishResult",
]
