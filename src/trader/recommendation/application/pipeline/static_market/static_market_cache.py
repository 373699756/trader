"""Resident issuer projection of accepted references; no supplier or history I/O."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime

from trader.recommendation.application.ports.static_reference import StaticReferenceRead
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.evidence.pipeline import SourceHealth, SourceHealthState
from trader.recommendation.domain.market.static import StaticIssuer


@dataclass(frozen=True, slots=True)
class StaticIssuerBaseline:
    records: tuple[StaticIssuer, ...]
    reference_epoch: str
    identity: str
    latest_success_at: datetime | None
    source_count: int

    def health(
        self, observed_at: datetime, *, refresh_failed: bool = False, ttl_seconds: float = 86_400
    ) -> SourceHealth:
        usable = self.latest_success_at is not None and self.latest_success_at <= observed_at
        stale = (
            self.latest_success_at is not None and (observed_at - self.latest_success_at).total_seconds() >= ttl_seconds
        )
        healthy = int(usable and not refresh_failed and not stale)
        return SourceHealth(
            SourceHealthState.READY
            if healthy
            else (SourceHealthState.DEGRADED if usable else SourceHealthState.UNAVAILABLE),
            self.source_count,
            healthy,
            self.latest_success_at,
            max(0.0, (observed_at - self.latest_success_at).total_seconds())
            if self.latest_success_at is not None
            else None,
        )


@dataclass(frozen=True, slots=True)
class StaticBaselineRead:
    baseline: StaticIssuerBaseline
    cache_hit: bool


class StaticMarketCache:
    """Reuse the accepted official population independently of quote availability."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._baseline: StaticIssuerBaseline | None = None
        self._sequence = 0

    def read(self, source: StaticReferenceRead, observed_at: datetime) -> StaticBaselineRead:
        reference = source.reference
        if reference is not None and reference.source_time > observed_at:
            reference = None
        records = reference.records if reference is not None else ()
        reference_epoch = source.reference_epoch
        source_time = reference.source_time.astimezone(SHANGHAI) if reference is not None else None
        with self._lock:
            previous = self._baseline
            if previous is not None and previous.reference_epoch == reference_epoch and previous.records == records:
                if previous.latest_success_at == source_time:
                    return StaticBaselineRead(previous, True)
                baseline = StaticIssuerBaseline(records, reference_epoch, previous.identity, source_time, 1)
                self._baseline = baseline
                return StaticBaselineRead(baseline, True)
            self._sequence += 1
            identity = f"static:{self._sequence}"
            baseline = StaticIssuerBaseline(
                records,
                reference_epoch,
                identity,
                source_time,
                1,
            )
            self._baseline = baseline
            return StaticBaselineRead(baseline, False)


__all__ = ["StaticMarketCache", "StaticBaselineRead", "StaticIssuerBaseline"]
