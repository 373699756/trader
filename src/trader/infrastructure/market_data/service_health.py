"""Unified market-data health and canonical snapshot metadata."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import _quote_age_summary


class MarketHealthMixin(MarketServiceState):
    def health(self) -> Mapping[str, object]:
        measured_at = self._wall_clock()
        with self._lock:
            market_quotes = tuple(feature.quote for feature in self._market_features)
            candidate_quotes = tuple(self._candidate_quotes.values())
            history_entries = len(self._history)
            market_cached = len(self._market_features)
            candidate_cached = len(self._candidate_quotes)
            research_entries = len(self._research)
            intraday_entries = len(self._intraday)
            research_success_count = self._research_success_count
            research_error_count = self._research_error_count
            research_planned_count = self._research_planned_count
            research_timeout_count = self._research_timeout_count
            research_consecutive_failures = self._research_consecutive_failures
            research_circuit_open = self._research_open_until > self._monotonic()
            research_latencies_ms = tuple(self._research_latencies_ms)
            research_latest_source_time = self._research_latest_source_time
            research_last_error = self._research_last_error
            intraday_success_count = self._intraday_success_count
            intraday_error_count = self._intraday_error_count
            intraday_last_error = self._intraday_last_error
            intraday_requested_rows = self._intraday_requested_rows
            intraday_covered_rows = self._intraday_covered_rows
            intraday_latest_source_time = self._intraday_latest_source_time
            intraday_sources = self._intraday_sources
            intraday_data_versions = self._intraday_data_versions
            history_universe_rows = self._history_universe_rows
            history_covered_rows = self._history_covered_rows
            history_error_count = self._history_error_count
            history_data_versions = self._history_data_versions
            quote_out_of_order_count = self._quote_out_of_order_count
            research_out_of_order_count = self._research_out_of_order_count
            history_out_of_order_count = self._history_out_of_order_count
            intraday_out_of_order_count = self._intraday_out_of_order_count
        gateway_health = dict(self._gateway.health())
        raw_sources = gateway_health.get("sources")
        sources = dict(raw_sources) if isinstance(raw_sources, Mapping) else {}
        sources["akshare"] = {
            "planned_count": research_planned_count,
            "success_count": research_success_count,
            "error_count": research_error_count,
            "timeout_count": research_timeout_count,
            "consecutive_failures": research_consecutive_failures,
            "circuit_open": research_circuit_open,
            "last_latency_ms": round(research_latencies_ms[-1], 2) if research_latencies_ms else None,
            "p50_latency_ms": _latency_percentile(research_latencies_ms, 0.50),
            "p95_latency_ms": _latency_percentile(research_latencies_ms, 0.95),
            "last_error": research_last_error,
            "data_age_seconds": max(0.0, (measured_at - research_latest_source_time).total_seconds())
            if research_latest_source_time is not None
            else None,
        }
        if self._tushare_client is not None:
            tushare_health = dict(self._tushare_client.health())
            sources["tushare"] = {
                **tushare_health,
                "last_error": tushare_health.get("degraded_reason"),
            }
        gateway_health["sources"] = sources
        return {
            **gateway_health,
            "history_cache_entries": history_entries,
            "market_feature_rows": market_cached,
            "candidate_quote_cache_entries": candidate_cached,
            "research_cache_entries": research_entries,
            "research_success_count": research_success_count,
            "research_error_count": research_error_count,
            "research_last_error": research_last_error,
            "intraday_tail_cache_entries": intraday_entries,
            "intraday_tail_success_count": intraday_success_count,
            "intraday_tail_error_count": intraday_error_count,
            "intraday_tail_last_error": intraday_last_error,
            "intraday_tail_requested_rows": intraday_requested_rows,
            "intraday_tail_covered_rows": intraday_covered_rows,
            "intraday_tail_coverage_ratio": intraday_covered_rows / intraday_requested_rows
            if intraday_requested_rows
            else 0.0,
            "intraday_tail_latest_source_time": intraday_latest_source_time,
            "intraday_tail_sources": intraday_sources,
            "intraday_tail_data_versions": intraday_data_versions,
            "history_universe_rows": history_universe_rows,
            "history_covered_rows": history_covered_rows,
            "history_coverage_ratio": history_covered_rows / history_universe_rows if history_universe_rows else 0.0,
            "history_error_count": history_error_count,
            "history_data_versions": history_data_versions,
            "quote_out_of_order_count": quote_out_of_order_count,
            "research_out_of_order_count": research_out_of_order_count,
            "history_out_of_order_count": history_out_of_order_count,
            "intraday_out_of_order_count": intraday_out_of_order_count,
            "market_quote_age": _quote_age_summary(market_quotes, measured_at),
            "candidate_quote_age": _quote_age_summary(candidate_quotes, measured_at),
            "measured_at": measured_at.isoformat(),
        }

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> Mapping[str, object]:
        snapshot = self._gateway.canonical_snapshot()
        if snapshot is None:
            return {}
        selected = set(codes) if codes is not None else None
        field_sources = {
            code: dict(sources)
            for code, sources in snapshot.field_sources.items()
            if selected is None or code in selected
        }
        conflicts = [
            conflict for conflict in snapshot.conflicts if selected is None or conflict.rpartition(":")[2] in selected
        ]
        missing_reasons = {
            key: reason
            for key, reason in snapshot.missing_reasons.items()
            if selected is None or key.partition(".")[0] in selected
        }
        with self._lock:
            tushare_reference_versions = dict(self._tushare_reference_versions)
        return {
            "merge_epoch": snapshot.merge_epoch,
            "source_versions": dict(snapshot.source_versions),
            "field_sources": field_sources,
            "market_conflicts": conflicts,
            "market_missing_reasons": missing_reasons,
            "market_degraded_reasons": list(snapshot.degraded_reasons),
            "market_observed_at": snapshot.observed_at.isoformat(),
            "tushare_reference_versions": tushare_reference_versions,
        }


def _latency_percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 2)


__all__ = ["MarketHealthMixin"]
