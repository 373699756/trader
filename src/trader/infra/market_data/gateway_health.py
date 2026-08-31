"""Typed internal health state for the composed market-data gateway."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from trader.application.cache import CacheStatus
from trader.application.latency import LatencyWaterfallStatus
from trader.application.source_lanes import SourceLaneRegistryStatus
from trader.domain.market.models import CanonicalMarketSnapshot
from trader.infra.market_data.normalization.columnar import MarketChangeSet
from trader.infra.market_data.router import RouteOutcome


@dataclass(frozen=True)
class SecurityMasterHealthStatus:
    total_rows: int
    listing_date_rows: int
    listing_age_rows: int
    complete_rows: int
    provider: str
    tushare_required: bool
    persistence_schedule_error_count: int


@dataclass(frozen=True)
class MarketSourceHealthStatus:
    planned_count: int
    success_count: int
    error_count: int
    timeout_count: int
    physical_failure_count: int
    circuit_skipped_count: int
    superseded_count: int
    recovery_probe_count: int
    recovery_probe_success_count: int
    consecutive_failures: int
    circuit_open: bool
    last_latency_ms: float
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    last_error: str
    data_age_seconds: float | None


@dataclass(frozen=True)
class MarketGatewayHealthStatus:
    active_source: str
    cached_rows: int
    merge_count: int
    conflict_count: int
    snapshot: CanonicalMarketSnapshot | None
    changes: MarketChangeSet
    route: RouteOutcome | None
    source_lanes: SourceLaneRegistryStatus | None
    security_master: SecurityMasterHealthStatus
    sources: Mapping[str, MarketSourceHealthStatus]
    cache: CacheStatus | None
    latency_waterfall: LatencyWaterfallStatus

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", MappingProxyType(dict(self.sources)))


__all__ = [
    "MarketGatewayHealthStatus",
    "MarketSourceHealthStatus",
    "SecurityMasterHealthStatus",
]
