"""Pure routing, health and single-flight runtime for the market gateway."""

from __future__ import annotations

import math
import threading
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from hashlib import sha256
from typing import Generic, TypeVar, cast
from uuid import uuid4

from trader.application.cache import CacheStatus, canonical_json_bytes
from trader.application.latency import LatencyWaterfall
from trader.application.ports.market import MarketDataNoDataError
from trader.application.source_lanes import SourceLaneRegistry
from trader.domain.market.models import (
    CanonicalMarketSnapshot,
    MarketQuote,
)
from trader.infra.failures import classify_adapter_failure
from trader.infra.market_data.merge import overlay_canonical_snapshot, subset_canonical_snapshot
from trader.infra.market_data.merge_quote import source_name, source_priority
from trader.infra.market_data.observations import SourceObservation
from trader.infra.market_data.router import RouteOutcome, VendorResult, VendorSeverity

_T = TypeVar("_T")


@dataclass
class _CircuitState:
    failures: int = 0
    open_until: float = 0.0
    success_count: int = 0
    error_count: int = 0
    last_latency_ms: float = 0.0
    last_error: str = ""
    planned_count: int = 0
    timeout_count: int = 0
    physical_failure_count: int = 0
    circuit_skipped_count: int = 0
    superseded_count: int = 0
    recovery_probe_count: int = 0
    recovery_probe_success_count: int = 0
    last_source_time: datetime | None = None
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=256))


@dataclass(frozen=True)
class _SourceFetch:
    name: str
    status: str
    observations: tuple[SourceObservation, ...] = ()
    error: str = ""
    skipped: bool = False
    duration_ms: float = 0.0


class _SingleFlight(Generic[_T]):
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._running = False
        self._generation = 0
        self._result: _T | None = None
        self._error: BaseException | None = None

    def run(self, function: Callable[[], _T]) -> _T:
        with self._condition:
            generation = self._generation
            if self._running:
                while self._running and self._generation == generation:
                    self._condition.wait()
                if self._error is not None:
                    raise self._error
                return cast(_T, self._result)
            self._running = True
        try:
            result = function()
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._result = None
                self._running = False
                self._generation += 1
                self._condition.notify_all()
            raise
        with self._condition:
            self._result = result
            self._error = None
            self._running = False
            self._generation += 1
            self._condition.notify_all()
        return result


def _route_health(last_route: RouteOutcome | None) -> Mapping[str, object]:
    if not isinstance(last_route, RouteOutcome):
        return {
            "status": "idle",
            "used_vendor": None,
            "degraded": False,
            "fallback_reason": None,
            "attempted_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "no_data_count": 0,
            "skipped_count": 0,
            "attempted_vendors": (),
        }
    attempted_vendors = [
        {
            "name": vendor.name,
            "status": vendor.status,
            "severity": vendor.severity.value,
            "error": vendor.error,
            "skipped": vendor.skipped,
            "duration_ms": round(vendor.duration_ms, 2) if vendor.duration_ms is not None else None,
        }
        for vendor in last_route.results
    ]
    return {
        "status": last_route.status,
        "used_vendor": last_route.vendor or None,
        "degraded": last_route.degraded,
        "fallback_reason": last_route.fallback_reason,
        "attempted_count": len(last_route.results),
        "success_count": sum(1 for vendor in last_route.results if vendor.status == "success"),
        "failure_count": sum(1 for vendor in last_route.results if vendor.status == "failed"),
        "no_data_count": sum(1 for vendor in last_route.results if vendor.status == "no_data"),
        "skipped_count": sum(1 for vendor in last_route.results if vendor.skipped),
        "attempted_vendors": attempted_vendors,
    }


def _parallel_route_outcome(results: Sequence[_SourceFetch]) -> RouteOutcome:
    successes = [result for result in results if result.status == "success"]
    no_data = any(result.status == "no_data" for result in results)
    route_results = tuple(
        VendorResult(
            name=result.name,
            status=result.status,
            severity=VendorSeverity.REQUIRED,
            result=result.observations if result.status == "success" else None,
            error=result.error,
            skipped=result.skipped,
            duration_ms=result.duration_ms,
        )
        for result in results
    )
    if successes:
        vendor = "eastmoney" if any(result.name == "eastmoney" for result in successes) else successes[0].name
        expected_skips = {"hedge_not_needed", "hedge_cancelled"}
        degraded = any(result.status != "success" and result.error not in expected_skips for result in results)
        fallback_reason = None
        if vendor == "sina":
            eastmoney = next((result for result in results if result.name == "eastmoney"), None)
            fallback_reason = (
                "hedge_delay" if eastmoney is not None and eastmoney.error == "hedge_inflight" else "primary_failed"
            )
        return RouteOutcome(
            result=tuple(observation for result in successes for observation in result.observations),
            vendor=vendor,
            results=route_results,
            degraded=degraded,
            status="success",
            fallback_reason=fallback_reason,
        )
    return RouteOutcome(
        result=None,
        vendor="" if no_data else results[-1].name,
        results=route_results,
        degraded=True,
        status="no_data" if no_data else "failed",
        fallback_reason="no_data" if no_data else "failed",
    )


def _parallel_error_message(results: Sequence[_SourceFetch]) -> str:
    no_data = [result for result in results if result.status == "no_data"]
    failures = [result for result in results if result.status != "no_data"]
    if no_data:
        detail = "; ".join(f"{result.name}: {result.error}" for result in no_data)
        if failures:
            detail += "; upstream failures: " + "; ".join(f"{result.name}: {result.error}" for result in failures)
        return detail
    return "; ".join(f"{result.name}: {result.error}" for result in failures)


def _source_degraded_reasons(results: Sequence[_SourceFetch]) -> set[str]:
    reasons: set[str] = set()
    for result in results:
        if result.status == "success":
            continue
        if result.error in {"hedge_not_needed", "hedge_cancelled"}:
            continue
        error = result.error.lower()
        if result.status == "no_data":
            code = "no_data"
        elif "timeout" in error or "timed out" in error:
            code = "timeout"
        elif "late" in error or "deadline" in error:
            code = "late"
        elif result.skipped and "circuit" in error:
            code = "circuit_open"
        elif result.skipped and "superseded" in error:
            code = "superseded"
        elif result.skipped and "hedge_inflight" in error:
            code = "hedge_delay"
        else:
            code = "source_failed"
        reasons.add(f"{result.name}:{code}")
    return reasons


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * quantile) - 1))
    return round(float(ordered[index]), 2)


def _canonical_health(snapshot: CanonicalMarketSnapshot | None) -> Mapping[str, object]:
    if snapshot is None:
        return {
            "observed_at": None,
            "merge_epoch": None,
            "source_versions": {},
            "conflicts": (),
            "missing_reasons": {},
            "degraded_reasons": (),
        }
    return {
        "observed_at": snapshot.observed_at.isoformat(),
        "merge_epoch": snapshot.merge_epoch,
        "source_versions": dict(snapshot.source_versions),
        "conflicts": snapshot.conflicts,
        "missing_reasons": dict(snapshot.missing_reasons),
        "degraded_reasons": snapshot.degraded_reasons,
    }


def _source_lane_status(registry: SourceLaneRegistry | None) -> dict[str, object]:
    if registry is None:
        return {}
    status = registry.status()
    return {
        source: {
            "source": lane.source,
            "running": lane.running,
            "pending": lane.pending,
            "completed_count": lane.completed_count,
            "coalesced_count": lane.coalesced_count,
            "superseded_count": lane.superseded_count,
            "rejected_count": lane.rejected_count,
            "stopped": lane.stopped,
        }
        for source, lane in status.lanes.items()
    }


def _latency_status(waterfall: LatencyWaterfall) -> dict[str, object]:
    status = waterfall.status()
    return {
        "sample_capacity": status.sample_capacity,
        "trace_capacity": status.trace_capacity,
        "stage_capacity": status.stage_capacity,
        "active_trace_count": status.active_trace_count,
        "planned_count": status.planned_count,
        "completed_count": status.completed_count,
        "failed_count": status.failed_count,
        "timeout_count": status.timeout_count,
        "superseded_count": status.superseded_count,
        "dropped_count": status.dropped_count,
        "dropped_stage_count": status.dropped_stage_count,
        "stages": {
            name: {
                "sample_count": stage.sample_count,
                "p50_ms": stage.p50_ms,
                "p95_ms": stage.p95_ms,
                "maximum_ms": stage.maximum_ms,
            }
            for name, stage in status.stages.items()
        },
    }


def _cache_status(status: CacheStatus) -> dict[str, object]:
    return {
        dataset: {
            source: {
                "entries": item.entries,
                "capacity": item.capacity,
                "hit": item.hit,
                "miss": item.miss,
                "refresh_due_hit": item.refresh_due_hit,
                "stale_hit": item.stale_hit,
                "degraded_hit": item.degraded_hit,
                "negative_hit": item.negative_hit,
                "refresh": item.refresh,
                "eviction": item.eviction,
                "load_error": item.load_error,
                "hit_rate": round(item.hit_rate, 6),
                "estimated_bytes": item.estimated_bytes,
            }
            for source, item in sources.items()
        }
        for dataset, sources in status.datasets.items()
    }


def _strip_source(source: str, message: str) -> str:
    prefix = f"{source}:"
    return message[len(prefix) :].strip() if message.startswith(prefix) else message


def _elapsed(started: float, finished: float) -> float:
    return max(0.0, (finished - started) * 1000.0)


def _cycle_trace_id(kind: str, observed_at: datetime, codes: Sequence[str]) -> str:
    payload = {
        "kind": kind,
        "observed_at": observed_at.isoformat(),
        "codes": tuple(sorted(set(codes))),
    }
    return sha256(canonical_json_bytes(payload) + uuid4().bytes).hexdigest()[:24]


def _before_deadline(now: datetime, deadline: datetime | None) -> bool:
    return deadline is None or now < deadline


def _cache_error_code(exc: Exception) -> str:
    if isinstance(exc, MarketDataNoDataError):
        return "no_data"
    failure = classify_adapter_failure(exc, provider="market", operation="source_fetch")
    return "late" if failure.code.value == "deadline" else failure.code.value


def _observation_version(observation: SourceObservation) -> tuple[datetime, datetime, str, str]:
    return (
        observation.source_time,
        observation.received_at,
        observation.data_version,
        observation.payload_hash,
    )


def _reference_replaces(current: SourceObservation, incoming: SourceObservation) -> bool:
    current_priority = source_priority(current.source)
    incoming_priority = source_priority(incoming.source)
    if current_priority != incoming_priority:
        return incoming_priority > current_priority
    current_order = (current.source_time, current.received_at, current.data_version)
    incoming_order = (incoming.source_time, incoming.received_at, incoming.data_version)
    if incoming_order != current_order:
        return incoming_order > current_order
    current_degraded = current.fields.get("reference_data_degraded") is True
    incoming_degraded = incoming.fields.get("reference_data_degraded") is True
    if current_degraded != incoming_degraded:
        return incoming_degraded
    return incoming.payload_hash >= current.payload_hash


def _preserve_newer_quotes(
    current: CanonicalMarketSnapshot,
    previous: CanonicalMarketSnapshot | None,
) -> CanonicalMarketSnapshot:
    if previous is None:
        return current
    overlay_codes = _newer_previous_quote_codes(current, previous)
    if not overlay_codes:
        return current
    overlay = subset_canonical_snapshot(previous, overlay_codes)
    return overlay_canonical_snapshot(current, replace(overlay, degraded_reasons=()))


def _newer_previous_quote_codes(
    current: CanonicalMarketSnapshot,
    previous: CanonicalMarketSnapshot,
) -> tuple[str, ...]:
    current_quotes = {quote.code: quote for quote in current.quotes}
    selected: list[str] = []
    for quote in previous.quotes:
        current_quote = current_quotes.get(quote.code)
        if current_quote is None or _previous_quote_is_newer(quote, current_quote):
            selected.append(quote.code)
    return tuple(selected)


def _previous_quote_is_newer(previous: MarketQuote, current: MarketQuote) -> bool:
    previous_time = (previous.source_time, previous.received_time)
    current_time = (current.source_time, current.received_time)
    if previous_time != current_time:
        return previous_time > current_time
    previous_source = source_name(previous.source)
    current_source = source_name(current.source)
    if previous_source == current_source:
        return previous.data_version > current.data_version
    return (source_priority(previous_source), previous_source) > (
        source_priority(current_source),
        current_source,
    )


__all__ = [
    "_CircuitState",
    "_SingleFlight",
    "_SourceFetch",
    "_before_deadline",
    "_cache_error_code",
    "_cache_status",
    "_canonical_health",
    "_elapsed",
    "_latency_status",
    "_observation_version",
    "_parallel_error_message",
    "_parallel_route_outcome",
    "_percentile",
    "_preserve_newer_quotes",
    "_reference_replaces",
    "_route_health",
    "_source_degraded_reasons",
    "_source_lane_status",
    "_strip_source",
]
