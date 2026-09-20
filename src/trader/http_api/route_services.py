"""Injected read-only services used by Flask."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trader.recommendation.application.pipeline.freeze_publish.event_stream import UnifiedDecisionEventStream
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import UnifiedDecisionQueries

StatusProvider = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class WebApiConfig:
    heartbeat_seconds: float = 15.0
    snapshot_retention_seconds: float = 0.0


@dataclass(frozen=True)
class UnifiedWebServices:
    queries: UnifiedDecisionQueries
    events: UnifiedDecisionEventStream
    status_provider: StatusProvider
    config: WebApiConfig = WebApiConfig()


__all__ = ["StatusProvider", "UnifiedWebServices", "WebApiConfig"]
