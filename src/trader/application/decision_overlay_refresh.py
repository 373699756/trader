"""Refresh mutable quote overlays without changing scored decision identity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trader.application.decision_core import UnifiedDecisionIndex
from trader.application.ports.v2_runtime import (
    V2CycleRequest,
    V2DecisionBuilderPort,
    V2DecisionUnavailableError,
)
from trader.domain.recommendation.decision_identity import DecisionOverlay, ScoredDecision
from trader.domain.recommendation.models import Strategy

OverlayFailureRecorder = Callable[[str, str, Strategy | None], None]
FailureCodeMapper = Callable[[BaseException, str], str]


@dataclass(frozen=True)
class DecisionOverlayRefresher:
    """Coordinate overlay CAS and event delivery for all scored strategies."""

    index: UnifiedDecisionIndex
    decisions: V2DecisionBuilderPort
    publish_overlay: Callable[[DecisionOverlay], object]
    record_failure: OverlayFailureRecorder
    failure_code: FailureCodeMapper

    def refresh(self, request: V2CycleRequest) -> None:
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            self._refresh_strategy(strategy, request)

    def _refresh_strategy(self, strategy: Strategy, request: V2CycleRequest) -> None:
        snapshot = self.index.snapshot(strategy)
        current = snapshot.current
        if not isinstance(current, ScoredDecision) or current.trade_date != request.trade_date:
            return
        try:
            overlay = self.decisions.refreshed_overlay(current, request, snapshot.overlay)
        except V2DecisionUnavailableError as exc:
            self.record_failure("overlay", self.failure_code(exc, "overlay_unavailable"), strategy)
            return
        if overlay is None:
            return
        expected = snapshot.overlay.version if snapshot.overlay is not None else None
        result = self.index.publish_overlay(overlay, expected_version=expected)
        if not result.accepted:
            if result.reason not in {
                "overlay_cas_mismatch",
                "overlay_conflict",
                "parent_mismatch",
                "stale_overlay",
            }:
                self.record_failure("overlay", result.reason, strategy)
            return
        try:
            self.publish_overlay(overlay)
        except (RuntimeError, TypeError, ValueError) as exc:
            self.record_failure("overlay", f"overlay_event:{type(exc).__name__}", strategy)


__all__ = ["DecisionOverlayRefresher"]
