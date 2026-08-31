"""Health aggregation for composed market-data components."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from trader.application.ports.market import MarketSnapshotMetadata
from trader.application.ports.types import JsonInput, JsonObject, freeze_json_object
from trader.infra.market_data.history.service_history import HistoryCache
from trader.infra.market_data.history.service_history_warmup import HistoryWarmup
from trader.infra.market_data.providers.tushare import TushareHealthStatus
from trader.infra.market_data.service.gateway_health import MarketGatewayHealthStatus, MarketSourceHealthStatus
from trader.infra.market_data.service.market_cache_identity import _quote_age_summary
from trader.infra.market_data.service.router import RouteOutcome
from trader.infra.market_data.service.service_candidates import QuoteCache
from trader.infra.market_data.service.service_intraday import IntradayLoader
from trader.infra.market_data.service.service_research import ResearchLoader
from trader.infra.market_data.service.service_tushare import ReferenceLoader


@dataclass(frozen=True)
class MarketDataHealthDependencies:
    quotes: QuoteCache
    history: HistoryCache
    warmup: HistoryWarmup
    research: ResearchLoader
    intraday: IntradayLoader
    references: ReferenceLoader


class MarketDataHealth:
    def __init__(
        self,
        dependencies: MarketDataHealthDependencies,
        *,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._quotes = dependencies.quotes
        self._history = dependencies.history
        self._warmup = dependencies.warmup
        self._research = dependencies.research
        self._intraday = dependencies.intraday
        self._references = dependencies.references
        self._wall_clock = wall_clock

    def health(self) -> JsonObject:
        measured_at = self._wall_clock()
        quote_status = self._quotes.status()
        history = self._history.status()
        warmup = self._warmup.status()
        research = self._research.status()
        intraday = self._intraday.status()
        gateway_status = self._quotes.gateway.health()
        gateway_health = _gateway_health_payload(gateway_status)
        sources = {
            name: _market_source_payload(source_status) for name, source_status in gateway_status.sources.items()
        }
        latencies = research.latencies_ms
        latest_research = research.latest_source_time
        sources["akshare"] = {
            "planned_count": research.planned_count,
            "success_count": research.success_count,
            "error_count": research.error_count,
            "timeout_count": research.timeout_count,
            "consecutive_failures": research.consecutive_failures,
            "circuit_open": research.circuit_open,
            "last_latency_ms": round(latencies[-1], 2) if latencies else None,
            "p50_latency_ms": _latency_percentile(latencies, 0.50),
            "p95_latency_ms": _latency_percentile(latencies, 0.95),
            "last_error": research.last_error,
            "data_age_seconds": max(0.0, (measured_at - latest_research).total_seconds())
            if isinstance(latest_research, datetime)
            else None,
        }
        reference_health = self._references.health()
        if reference_health is not None:
            sources["tushare"] = _tushare_payload(reference_health)
        security_master_health = self._references.security_master_health()
        if security_master_health is not None:
            sources["exchange"] = {
                "enabled": security_master_health.enabled,
                "planned_count": security_master_health.planned_count,
                "success_count": security_master_health.success_count,
                "error_count": security_master_health.error_count,
                "timeout_count": security_master_health.timeout_count,
                "consecutive_failures": security_master_health.consecutive_failures,
                "last_latency_ms": security_master_health.last_latency_ms,
                "p50_latency_ms": security_master_health.p50_latency_ms,
                "p95_latency_ms": security_master_health.p95_latency_ms,
                "last_error": security_master_health.last_error,
                "snapshot_rows": security_master_health.snapshot_rows,
                "listing_date_rows": security_master_health.listing_date_rows,
                "data_age_seconds": (
                    max(0.0, (measured_at - security_master_health.last_source_time).total_seconds())
                    if security_master_health.last_source_time is not None
                    else None
                ),
                "timeout_seconds": security_master_health.timeout_seconds,
            }
        gateway_health["sources"] = sources
        market_quotes = quote_status.market_features
        candidate_quotes = quote_status.candidate_quotes
        latest_candidate_quote = (
            max(candidate_quotes, key=lambda quote: (quote.source_time, quote.received_time, quote.data_version))
            if candidate_quotes
            else None
        )
        history_rows = history.universe_rows
        history_covered = history.covered_rows
        intraday_rows = intraday.requested_rows
        intraday_covered = intraday.covered_rows
        return freeze_json_object(
            cast(
                Mapping[str, JsonInput],
                {
                    **gateway_health,
                    "history_memory_entries": history.entries,
                    "history_raw_rows": history.raw_rows,
                    "history_profile_entries": history.profile_entries,
                    "market_feature_rows": quote_status.market_feature_rows,
                    "candidate_quote_cache_entries": quote_status.candidate_quote_entries,
                    "candidate_quote_latest_source": (
                        latest_candidate_quote.source if latest_candidate_quote is not None else None
                    ),
                    "research_cache_entries": research.entries,
                    "research_success_count": research.success_count,
                    "research_error_count": research.error_count,
                    "research_last_error": research.last_error,
                    "research_verified_count": research.verified_count,
                    "research_partial_count": research.partial_count,
                    "research_unavailable_count": research.unavailable_count,
                    "research_component_coverage": {
                        "financial": research.financial_covered_count,
                        "announcements": research.announcements_covered_count,
                        "pledge": research.pledge_covered_count,
                        "unlock": research.unlock_covered_count,
                    },
                    "corporate_risk_registry_covered_count": research.corporate_risk_covered_count,
                    "corporate_risk_registry_fact_count": research.corporate_risk_fact_count,
                    "corporate_risk_registry_versions": research.corporate_risk_registry_versions,
                    "intraday_tail_cache_entries": intraday.entries,
                    "intraday_tail_success_count": intraday.success_count,
                    "intraday_tail_error_count": intraday.error_count,
                    "intraday_tail_last_error": intraday.last_error,
                    "intraday_tail_requested_rows": intraday_rows,
                    "intraday_tail_covered_rows": intraday_covered,
                    "intraday_tail_coverage_ratio": intraday_covered / intraday_rows if intraday_rows else 0.0,
                    "intraday_tail_latest_source_time": intraday.latest_source_time,
                    "intraday_tail_sources": intraday.sources,
                    "intraday_tail_data_versions": intraday.data_versions,
                    "history_universe_rows": history_rows,
                    "history_covered_rows": history_covered,
                    "history_coverage_ratio": history_covered / history_rows if history_rows else 0.0,
                    "history_error_count": history.error_count,
                    "history_data_versions": history.data_versions,
                    "history_warmup_planned_count": warmup.planned_count,
                    "history_warmup_completed_count": warmup.completed_count,
                    "history_warmup_failure_count": warmup.failure_count,
                    "history_warmup_inflight_count": warmup.inflight_count,
                    "history_warmup_retry_deferred_count": warmup.retry_deferred_count,
                    "history_warmup_unique_failure_count": warmup.unique_failure_count,
                    "history_warmup_next_retry_seconds": warmup.next_retry_seconds,
                    "history_warmup_last_source": warmup.last_source or None,
                    "history_warmup_timeout_count": warmup.timeout_count,
                    "history_warmup_inflight_age_seconds": warmup.inflight_age_seconds,
                    "history_warmup_batch_timeout_seconds": warmup.batch_timeout_seconds,
                    "quote_out_of_order_count": quote_status.out_of_order_count,
                    "research_out_of_order_count": research.out_of_order_count,
                    "history_out_of_order_count": history.out_of_order_count,
                    "intraday_out_of_order_count": intraday.out_of_order_count,
                    "market_quote_age": _quote_age_summary(
                        tuple(feature.quote for feature in market_quotes), measured_at
                    ),
                    "candidate_quote_age": _quote_age_summary(candidate_quotes, measured_at),
                    "measured_at": measured_at.isoformat(),
                },
            )
        )

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> MarketSnapshotMetadata:
        snapshot = self._quotes.gateway.canonical_snapshot()
        if snapshot is None:
            return MarketSnapshotMetadata()
        selected = set(codes) if codes is not None else None
        return MarketSnapshotMetadata(
            merge_epoch=snapshot.merge_epoch,
            source_versions=snapshot.source_versions,
            field_sources={
                code: dict(sources)
                for code, sources in snapshot.field_sources.items()
                if selected is None or code in selected
            },
            conflicts=tuple(
                conflict
                for conflict in snapshot.conflicts
                if selected is None or conflict.rpartition(":")[2] in selected
            ),
            missing_reasons={
                key: reason
                for key, reason in snapshot.missing_reasons.items()
                if selected is None or key.partition(".")[0] in selected
            },
            degraded_reasons=snapshot.degraded_reasons,
            observed_at=snapshot.observed_at,
            reference_versions=self._references.versions(),
        )


def _gateway_health_payload(status: MarketGatewayHealthStatus) -> dict[str, JsonInput]:
    snapshot = status.snapshot
    changes = status.changes
    security_master = status.security_master
    cache = status.cache
    latency = status.latency_waterfall
    return {
        "active_source": status.active_source,
        "cached_rows": status.cached_rows,
        "merge_count": status.merge_count,
        "conflict_count": status.conflict_count,
        "merge_epoch": snapshot.merge_epoch if snapshot is not None else None,
        "market_changes": {
            "merge_epoch": changes.merge_epoch,
            "inserted": len(changes.inserted_codes),
            "updated": len(changes.updated_codes),
            "removed": len(changes.removed_codes),
            "dirty": len(changes.dirty_codes),
        },
        "canonical_snapshot": {
            "observed_at": snapshot.observed_at.isoformat() if snapshot is not None else None,
            "merge_epoch": snapshot.merge_epoch if snapshot is not None else None,
            "source_versions": dict(snapshot.source_versions) if snapshot is not None else {},
            "conflicts": snapshot.conflicts if snapshot is not None else (),
            "missing_reasons": dict(snapshot.missing_reasons) if snapshot is not None else {},
            "degraded_reasons": snapshot.degraded_reasons if snapshot is not None else (),
        },
        "route": _route_payload(status.route),
        "source_lanes": (
            {
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
                for source, lane in status.source_lanes.lanes.items()
            }
            if status.source_lanes is not None
            else {}
        ),
        "security_master": {
            "total_rows": security_master.total_rows,
            "listing_date_rows": security_master.listing_date_rows,
            "listing_age_rows": security_master.listing_age_rows,
            "complete_rows": security_master.complete_rows,
            "provider": security_master.provider,
            "tushare_required": security_master.tushare_required,
            "persistence_schedule_error_count": security_master.persistence_schedule_error_count,
        },
        "cache": (
            {
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
                    for source, item in source_statuses.items()
                }
                for dataset, source_statuses in cache.datasets.items()
            }
            if cache is not None
            else {}
        ),
        "latency_waterfall": {
            "sample_capacity": latency.sample_capacity,
            "trace_capacity": latency.trace_capacity,
            "stage_capacity": latency.stage_capacity,
            "active_trace_count": latency.active_trace_count,
            "planned_count": latency.planned_count,
            "completed_count": latency.completed_count,
            "failed_count": latency.failed_count,
            "timeout_count": latency.timeout_count,
            "superseded_count": latency.superseded_count,
            "dropped_count": latency.dropped_count,
            "dropped_stage_count": latency.dropped_stage_count,
            "stages": {
                name: {
                    "sample_count": stage.sample_count,
                    "p50_ms": stage.p50_ms,
                    "p95_ms": stage.p95_ms,
                    "maximum_ms": stage.maximum_ms,
                }
                for name, stage in latency.stages.items()
            },
        },
    }


def _route_payload(route: RouteOutcome | None) -> dict[str, JsonInput]:
    if route is None:
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
    return {
        "status": route.status,
        "used_vendor": route.vendor or None,
        "degraded": route.degraded,
        "fallback_reason": route.fallback_reason,
        "attempted_count": len(route.results),
        "success_count": sum(1 for vendor in route.results if vendor.status == "success"),
        "failure_count": sum(1 for vendor in route.results if vendor.status == "failed"),
        "no_data_count": sum(1 for vendor in route.results if vendor.status == "no_data"),
        "skipped_count": sum(1 for vendor in route.results if vendor.skipped),
        "attempted_vendors": [
            {
                "name": vendor.name,
                "status": vendor.status,
                "severity": vendor.severity.value,
                "error": vendor.error,
                "skipped": vendor.skipped,
                "duration_ms": round(vendor.duration_ms, 2) if vendor.duration_ms is not None else None,
            }
            for vendor in route.results
        ],
    }


def _market_source_payload(status: MarketSourceHealthStatus) -> dict[str, JsonInput]:
    return {
        "planned_count": status.planned_count,
        "success_count": status.success_count,
        "error_count": status.error_count,
        "timeout_count": status.timeout_count,
        "physical_failure_count": status.physical_failure_count,
        "circuit_skipped_count": status.circuit_skipped_count,
        "superseded_count": status.superseded_count,
        "recovery_probe_count": status.recovery_probe_count,
        "recovery_probe_success_count": status.recovery_probe_success_count,
        "consecutive_failures": status.consecutive_failures,
        "circuit_open": status.circuit_open,
        "last_latency_ms": status.last_latency_ms,
        "p50_latency_ms": status.p50_latency_ms,
        "p95_latency_ms": status.p95_latency_ms,
        "last_error": status.last_error,
        "data_age_seconds": status.data_age_seconds,
    }


def _tushare_payload(status: TushareHealthStatus) -> dict[str, JsonInput]:
    return {
        "enabled": status.enabled,
        "access_points": status.access_points,
        "history_mode": status.history_mode,
        "minute_call_limit": status.minute_call_limit,
        "daily_call_limit": status.daily_call_limit,
        "process_api_attempts_last_minute": status.process_api_attempts_last_minute,
        "process_api_attempts_today": status.process_api_attempts_today,
        "process_remaining_calls_today": status.process_remaining_calls_today,
        "local_rate_limit_count": status.local_rate_limit_count,
        "planned_count": status.planned_count,
        "success_count": status.success_count,
        "error_count": status.error_count,
        "consecutive_failures": status.consecutive_failures,
        "circuit_open": status.circuit_open,
        "timeout_count": status.timeout_count,
        "last_latency_ms": status.last_latency_ms,
        "p50_latency_ms": status.p50_latency_ms,
        "p95_latency_ms": status.p95_latency_ms,
        "degraded_reason": status.degraded_reason,
        "last_error": status.degraded_reason,
        "timeout_seconds": status.timeout_seconds,
        "data_age_seconds": status.data_age_seconds,
    }


def _latency_percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 2)


__all__ = ["MarketDataHealth"]
