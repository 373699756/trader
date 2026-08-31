"""Refresh mutable quote overlays without changing scored decision identity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from trader.application.decisions.decision_core import UnifiedDecisionIndex
from trader.application.ports.v2_runtime import (
    V2CycleRequest,
    V2DecisionBuilderPort,
    V2DecisionUnavailableError,
)
from trader.domain.recommendation.decision_identity import DecisionOverlay, ScoredDecision
from trader.domain.recommendation.models import Strategy

FailureCodeMapper = Callable[[BaseException, str], str]


@dataclass(frozen=True)
class OverlayRefreshOutcome:
    strategy: Strategy
    status: Literal["skipped", "succeeded", "published", "failed"]
    error_code: str = ""

    def __post_init__(self) -> None:
        if (self.status == "failed") != bool(self.error_code):
            raise ValueError("failed overlay outcomes must carry exactly one error code")


@dataclass(frozen=True)
class DecisionOverlayRefresher:
    """Coordinate overlay CAS and event delivery for all scored strategies."""

    index: UnifiedDecisionIndex
    decisions: V2DecisionBuilderPort
    publish_overlay: Callable[[DecisionOverlay], object]
    failure_code: FailureCodeMapper

    def refresh(self, request: V2CycleRequest) -> tuple[OverlayRefreshOutcome, ...]:
        return tuple(self._refresh_strategy(strategy, request) for strategy in _SCORED_STRATEGIES)

    def _refresh_strategy(self, strategy: Strategy, request: V2CycleRequest) -> OverlayRefreshOutcome:
        snapshot = self.index.snapshot(strategy)
        current = snapshot.current
        if not isinstance(current, ScoredDecision) or current.trade_date != request.trade_date:
            return OverlayRefreshOutcome(strategy, "skipped")
        try:
            overlay = self.decisions.refreshed_overlay(current, request, snapshot.overlay)
        except V2DecisionUnavailableError as exc:
            return OverlayRefreshOutcome(strategy, "failed", self.failure_code(exc, "overlay_unavailable"))
        if overlay is None:
            return OverlayRefreshOutcome(strategy, "succeeded")
        return self._publish(strategy, overlay, expected_version=snapshot.overlay.version if snapshot.overlay else None)

    def _publish(
        self,
        strategy: Strategy,
        overlay: DecisionOverlay,
        *,
        expected_version: str | None,
    ) -> OverlayRefreshOutcome:
        result = self.index.publish_overlay(overlay, expected_version=expected_version)
        if not result.accepted:
            if result.reason in {
                "overlay_cas_mismatch",
                "overlay_conflict",
                "parent_mismatch",
                "stale_overlay",
            }:
                return OverlayRefreshOutcome(strategy, "succeeded")
            return OverlayRefreshOutcome(strategy, "failed", result.reason)
        try:
            self.publish_overlay(overlay)
        except (RuntimeError, TypeError, ValueError) as exc:
            return OverlayRefreshOutcome(strategy, "failed", f"overlay_event:{type(exc).__name__}")
        return OverlayRefreshOutcome(strategy, "published")


_SCORED_STRATEGIES = (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)


__all__ = ["DecisionOverlayRefresher", "OverlayRefreshOutcome"]
