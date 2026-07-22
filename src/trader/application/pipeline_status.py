"""Read-only runtime status and health persistence mixin."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, cast

from trader.application.cadence import PipelineTask, freshness_level
from trader.application.pipeline_state import PipelineState
from trader.application.pipeline_workers import worker_status
from trader.application.ports.types import JsonObject
from trader.application.schedule import MarketPhase
from trader.application.snapshot_workflow import topk_quote_age
from trader.domain.recommendation.models import Strategy

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_CRITICAL_TOPK_PHASES = {
    MarketPhase.TODAY_OBSERVE,
    MarketPhase.TODAY_MAIN,
    MarketPhase.FINAL_REVIEW,
    MarketPhase.DEEPSEEK_CUTOFF,
    MarketPhase.FINAL_QUOTE,
}


class PipelineStatusMixin(PipelineState):
    def status(self) -> dict[str, object]:
        measured_at = self._now()
        market_data: dict[str, object] = dict(self._market_metadata.health())
        try:
            phase = MarketPhase(self._state.current_phase())
        except ValueError:
            phase = MarketPhase.CLOSED
        is_trading_day = phase is not MarketPhase.CLOSED
        topk_target = 10.0 if phase in _CRITICAL_TOPK_PHASES else 20.0
        market_data["topk_quote_age"] = topk_quote_age(
            self._state,
            self._live_overlays,
            measured_at,
            target_seconds=topk_target,
        )
        market_data["freshness"] = self._freshness_status(
            market_data,
            measured_at,
            is_trading_day=is_trading_day,
        )
        cadence_status: dict[str, object] = (
            dict(self._cadence.status()) if self._cadence is not None else {"enabled": False}
        )
        with self._cadence_lock:
            cadence_status["inflight_tasks"] = sorted(task.value for task in self._scheduled_inflight)
        deepseek_status: dict[str, object] = (
            dict(self._reviews.status()) if self._reviews is not None else {"enabled": False}
        )
        deepseek_status["veto_count"] = sum(
            item.veto
            for strategy in Strategy
            if (snapshot := self._state.latest(strategy)) is not None
            for item in snapshot.recommendations
        )
        dependencies = {
            "market_data": market_data,
            "deepseek": deepseek_status,
            "event_queue": self._queue.status(),
            "worker_pools": worker_status(cast("RecommendationPipeline", self)),
            "cadence": cadence_status,
            "publisher": self._publisher.status(),
            "published_snapshots": dict(self._published_snapshots.status()),
            "persistent_audit": self._observability_status(),
        }
        return self._state.snapshot(dependencies)

    def _observability_status(self) -> JsonObject:
        try:
            return self._snapshot_observability.observability_status()
        except (OSError, RuntimeError, ValueError):
            return {"error": "persistent_observability_unavailable"}

    def _record_health_snapshot(self) -> None:
        health = dict(self._market_metadata.health())
        updated_at = self._now()
        if not self._persistence_running:
            self._snapshot_observability.record_data_source_health(health, updated_at=updated_at)
            return
        future = self._persistence_pool.submit(
            self._snapshot_observability.record_data_source_health,
            health,
            updated_at=updated_at,
        )
        if future is None:
            self._state.increment("observability_write_rejections")

    def _freshness_status(
        self,
        market_data: Mapping[str, object],
        measured_at: datetime,
        *,
        is_trading_day: bool,
    ) -> Mapping[str, object]:
        planner = self._cadence
        categories = {
            "full_market": (PipelineTask.FULL_MARKET, market_data.get("market_quote_age")),
            "candidate_quotes": (PipelineTask.CANDIDATE_QUOTES, market_data.get("candidate_quote_age")),
            "topk_quotes": (PipelineTask.TOPK_QUOTES, market_data.get("topk_quote_age")),
        }
        result: dict[str, object] = {}
        for name, (task, raw_summary) in categories.items():
            summary = raw_summary if isinstance(raw_summary, Mapping) else {}
            raw_age = summary.get("maximum_seconds")
            age = float(raw_age) if isinstance(raw_age, (int, float)) and not isinstance(raw_age, bool) else None
            interval = (
                planner.interval_for(task, measured_at, is_trading_day=is_trading_day) if planner is not None else None
            )
            result[name] = {
                "level": freshness_level(age, interval),
                "age_seconds": age,
                "interval_seconds": interval,
                "stale_after_seconds": interval * 2.0 if interval is not None else None,
                "degraded_after_seconds": interval * 3.0 if interval is not None else None,
            }
        return result
