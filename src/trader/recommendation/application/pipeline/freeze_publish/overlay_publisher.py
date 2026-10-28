"""Refresh quote overlays without changing the published freeze identity."""

from __future__ import annotations

import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import UnifiedFreezeIndex
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    FreezeBuilderPort,
    FreezeUnavailableError,
)
from trader.recommendation.application.runtime.trading_session import SessionGuard
from trader.recommendation.domain.publication.freeze_identity import FreezeOverlay, ScoredFreeze
from trader.recommendation.domain.publication.models import Strategy

FailureCodeMapper = Callable[[BaseException, str], str]
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class OverlayRefreshOutcome:
    strategy: Strategy
    status: Literal["skipped", "succeeded", "published", "failed"]
    error_code: str = ""

    def __post_init__(self) -> None:
        if (self.status == "failed") != bool(self.error_code):
            raise ValueError("failed overlay outcomes must carry exactly one error code")


@dataclass(frozen=True)
class FreezeOverlayRefresher:
    """Coordinate overlay CAS and event delivery for all scored strategies."""

    index: UnifiedFreezeIndex
    freezes: FreezeBuilderPort
    publish_overlay: Callable[[FreezeOverlay], object]
    failure_code: FailureCodeMapper

    def refresh(self, request: CycleRequest) -> tuple[OverlayRefreshOutcome, ...]:
        return tuple(self._refresh_strategy(strategy, request) for strategy in _SCORED_STRATEGIES)

    def _refresh_strategy(self, strategy: Strategy, request: CycleRequest) -> OverlayRefreshOutcome:
        snapshot = self.index.snapshot(strategy)
        current = snapshot.current
        if not isinstance(current, ScoredFreeze) or current.trade_date != request.trade_date:
            return OverlayRefreshOutcome(strategy, "skipped")
        try:
            overlay = self.freezes.refreshed_overlay(current, request, snapshot.overlay)
        except FreezeUnavailableError as exc:
            return OverlayRefreshOutcome(strategy, "failed", self.failure_code(exc, "overlay_unavailable"))
        except ValueError as exc:
            boundary = traceback.extract_tb(exc.__traceback__, limit=-1)
            if boundary:
                _LOGGER.warning(
                    "overlay refresh rejected: strategy=%s boundary=%s:%s",
                    strategy.value,
                    boundary[0].name,
                    boundary[0].lineno,
                )
            return OverlayRefreshOutcome(strategy, "failed", self.failure_code(exc, "overlay_invalid_quote"))
        if overlay is None:
            return OverlayRefreshOutcome(strategy, "succeeded")
        with SessionGuard(request.session):
            return self._publish(
                strategy, overlay, expected_version=snapshot.overlay.version if snapshot.overlay else None
            )

    def _publish(
        self,
        strategy: Strategy,
        overlay: FreezeOverlay,
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


_SCORED_STRATEGIES = (Strategy.TOMORROW, Strategy.D25)


__all__ = ["FreezeOverlayRefresher", "OverlayRefreshOutcome"]
