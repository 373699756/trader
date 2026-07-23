"""Injected read-only services used by Flask route groups."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from trader.application.publisher import SnapshotPublisher
from trader.application.queries import RecommendationQueries

StatusProvider = Callable[[], dict[str, object]]


@dataclass(frozen=True)
class WebApiConfig:
    default_top_n: int = 10
    maximum_top_n: int = 18
    default_event_limit: int = 100
    maximum_event_limit: int = 500
    heartbeat_seconds: float = 15.0


@dataclass(frozen=True)
class WebServices:
    status_provider: StatusProvider
    queries: RecommendationQueries | None = None
    publisher: SnapshotPublisher | None = None
    config: WebApiConfig = WebApiConfig()


__all__ = ["StatusProvider", "WebApiConfig", "WebServices"]
