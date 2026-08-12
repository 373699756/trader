"""Injected read-only V2 services used by Flask."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trader.application.decision_queries import UnifiedDecisionQueries
from trader.application.decision_stream import UnifiedDecisionEventStream

StatusProvider = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class WebApiConfig:
    heartbeat_seconds: float = 15.0


@dataclass(frozen=True)
class UnifiedWebServices:
    queries: UnifiedDecisionQueries
    events: UnifiedDecisionEventStream
    status_provider: StatusProvider
    config: WebApiConfig = WebApiConfig()


__all__ = ["StatusProvider", "UnifiedWebServices", "WebApiConfig"]
