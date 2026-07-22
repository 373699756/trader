"""Health aggregation for composed market-data components."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from trader.infra.market_data.service_candidates import QuoteStore
from trader.infra.market_data.service_history import HistoryStore
from trader.infra.market_data.service_history_warmup import HistoryWarmup
from trader.infra.market_data.service_intraday import IntradayLoader
from trader.infra.market_data.service_research import ResearchLoader
from trader.infra.market_data.service_support import _quote_age_summary
from trader.infra.market_data.service_tushare import ReferenceLoader


class MarketDataHealth:
    def __init__(
        self,
        quotes: QuoteStore,
        history: HistoryStore,
        warmup: HistoryWarmup,
        research: ResearchLoader,
        intraday: IntradayLoader,
        references: ReferenceLoader,
        *,
        wall_clock: Callable[[], datetime],
    ) -> None:
        self._quotes = quotes
        self._history = history
        self._warmup = warmup
        self._research = research
        self._intraday = intraday
        self._references = references
        self._wall_clock = wall_clock

    def health(self) -> Mapping[str, object]:
        measured_at = self._wall_clock()
        quote_status = self._quotes.status()
        history = self._history.status()
        warmup = self._warmup.status()
        research = self._research.status()
        intraday = self._intraday.status()
        gateway_health = dict(self._quotes.gateway.health())
        raw_sources = gateway_health.get("sources")
        sources = dict(raw_sources) if isinstance(raw_sources, Mapping) else {}
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
        if reference_health:
            sources["tushare"] = {
                **reference_health,
                "last_error": reference_health.get("degraded_reason"),
            }
        gateway_health["sources"] = sources
        market_quotes = quote_status.market_features
        candidate_quotes = quote_status.candidate_quotes
        history_rows = history.universe_rows
        history_covered = history.covered_rows
        intraday_rows = intraday.requested_rows
        intraday_covered = intraday.covered_rows
        return {
            **gateway_health,
            "history_cache_entries": history.entries,
            "market_feature_rows": quote_status.market_feature_rows,
            "candidate_quote_cache_entries": quote_status.candidate_quote_entries,
            "research_cache_entries": research.entries,
            "research_success_count": research.success_count,
            "research_error_count": research.error_count,
            "research_last_error": research.last_error,
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
            "history_warmup_last_source": warmup.last_source or None,
            "quote_out_of_order_count": quote_status.out_of_order_count,
            "research_out_of_order_count": research.out_of_order_count,
            "history_out_of_order_count": history.out_of_order_count,
            "intraday_out_of_order_count": intraday.out_of_order_count,
            "market_quote_age": _quote_age_summary(tuple(feature.quote for feature in market_quotes), measured_at),
            "candidate_quote_age": _quote_age_summary(candidate_quotes, measured_at),
            "measured_at": measured_at.isoformat(),
        }

    def snapshot_metadata(self, codes: Sequence[str] | None = None) -> Mapping[str, object]:
        snapshot = self._quotes.gateway.canonical_snapshot()
        if snapshot is None:
            return {}
        selected = set(codes) if codes is not None else None
        return {
            "merge_epoch": snapshot.merge_epoch,
            "source_versions": dict(snapshot.source_versions),
            "field_sources": {
                code: dict(sources)
                for code, sources in snapshot.field_sources.items()
                if selected is None or code in selected
            },
            "market_conflicts": [
                conflict
                for conflict in snapshot.conflicts
                if selected is None or conflict.rpartition(":")[2] in selected
            ],
            "market_missing_reasons": {
                key: reason
                for key, reason in snapshot.missing_reasons.items()
                if selected is None or key.partition(".")[0] in selected
            },
            "market_degraded_reasons": list(snapshot.degraded_reasons),
            "market_observed_at": snapshot.observed_at.isoformat(),
            "tushare_reference_versions": dict(self._references.versions()),
        }


def _latency_percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(quantile * len(ordered)) - 1)], 2)


__all__ = ["MarketDataHealth"]
