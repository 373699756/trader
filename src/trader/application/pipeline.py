"""Bounded recommendation pipeline and deterministic single-tick use case."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta

from trader.application.cadence import (
    CadencePlanner,
    CadencePolicy,
    PipelineTask,
    ScheduledPipelineTask,
    freshness_level,
)
from trader.application.events import (
    BoundedEventQueue,
    EventPriority,
    PipelineEvent,
    event_from_audit_record,
    new_event,
)
from trader.application.pipeline_stages import (
    persist,
    process_event_on_workers,
    worker_status,
)
from trader.application.ports import (
    DeepSeekReviewPort,
    EventAuditPort,
    MarketDataPort,
    SnapshotRepositoryPort,
    TradingCalendarPort,
)
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import (
    MarketPhase,
    SchedulePoint,
    decision_at,
    freeze_due_at,
    schedule_point_at,
    shanghai_now,
    trade_date_at,
)
from trader.application.snapshot_workflow import (
    freeze_available_snapshots,
    process_schedule,
    refresh_live_overlays,
    topk_quote_age,
)
from trader.application.status import RuntimeState
from trader.application.workers import BoundedExecutor
from trader.domain.models import (
    FeatureSnapshot,
    FilterAudit,
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)

_LOGGER = logging.getLogger(__name__)


class RecommendationPipeline:
    def __init__(
        self,
        market_data: MarketDataPort,
        calendar: TradingCalendarPort,
        reviews: DeepSeekReviewPort | None,
        repository: SnapshotRepositoryPort,
        event_audit: EventAuditPort,
        publisher: SnapshotPublisher,
        engine: RecommendationEngine,
        state: RuntimeState,
        *,
        config_version: str,
        candidate_pool_size: int,
        event_queue_size: int,
        priority_queue_size: int,
        now: Callable[[], datetime],
        market_workers: int = 6,
        normalization_workers: int = 2,
        strategy_workers: int = 3,
        deepseek_workers: int = 4,
        data_pool: BoundedExecutor | None = None,
        market_data_manages_workers: bool = False,
        cadence_policy: CadencePolicy | None = None,
        long_codes: Sequence[str] = (),
        long_target_prices: Mapping[str, float | None] | None = None,
    ) -> None:
        self._market_data = market_data
        self._calendar = calendar
        self._reviews = reviews
        self._repository = repository
        self._event_audit = event_audit
        self._publisher = publisher
        self._engine = engine
        self._state = state
        self._config_version = config_version
        self._candidate_pool_size = candidate_pool_size
        self._now = now
        self._long_codes = tuple(long_codes)
        self._long_target_prices = dict(long_target_prices or {})
        self._market_data_manages_workers = market_data_manages_workers
        self._cadence = CadencePlanner(cadence_policy) if cadence_policy is not None else None
        self._queue = BoundedEventQueue(
            maximum_size=event_queue_size,
            reserved_priority_size=priority_queue_size,
        )
        worker_queue_capacity = max(1, event_queue_size)
        self._data_pool = data_pool or BoundedExecutor(
            worker_count=market_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-data",
        )
        self._normalization_pool = BoundedExecutor(
            worker_count=normalization_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-normalize",
        )
        self._strategy_pool = BoundedExecutor(
            worker_count=strategy_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-strategy",
        )
        self._deepseek_pool = BoundedExecutor(
            worker_count=deepseek_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-deepseek",
        )
        self._long_pool = BoundedExecutor(
            worker_count=1,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-long",
        )
        self._persistence_pool = BoundedExecutor(
            worker_count=1,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-persistence",
        )
        self._compute_pools = (
            self._data_pool,
            self._normalization_pool,
            self._strategy_pool,
            self._deepseek_pool,
            self._long_pool,
        )
        self._lifecycle_lock = threading.Lock()
        self._cadence_lock = threading.Lock()
        self._merge_status_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._accepting = False
        self._persistence_running = False
        self._stopped = False
        self._merge_inflight = 0
        self._merge_submitted_count = 0
        self._merge_completed_count = 0
        self._candidate_codes: tuple[str, ...] = ()
        self._candidate_features: tuple[FeatureSnapshot, ...] = ()
        self._market_features: tuple[FeatureSnapshot, ...] = ()
        self._filter_reasons: Mapping[str, int] = {}
        self._filter_details: tuple[FilterAudit, ...] = ()
        self._filtered_count = 0
        self._frozen_keys: set[tuple[Strategy, str]] = set()
        self._live_overlays: dict[tuple[Strategy, str], LiveOverlay] = {}
        self._scheduled_inflight: set[PipelineTask] = set()

    def initialize(self) -> Mapping[str, int]:
        self._repository.initialize()
        recovery = self._repository.recover()
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            for trade_date in self._repository.recommendation_dates(strategy):
                key = (strategy, trade_date)
                self._frozen_keys.add(key)
                self._state.restore_frozen(strategy, trade_date)
        for strategy in Strategy:
            latest = self._repository.latest(strategy)
            if latest is not None:
                self._state.restore_snapshot(latest)
                overlay = self._repository.load_live_overlay(strategy, latest.trade_date)
                if overlay is not None and overlay.snapshot_id == latest.snapshot_id:
                    self._live_overlays[(strategy, latest.trade_date)] = overlay
        now = self._now()
        trade_day = trade_date_at(now)
        catchup = self._freeze_available_snapshots(
            now,
            freeze_due_at(now, is_trading_day=self._calendar.is_trading_day(trade_day)),
        )
        for record in self._event_audit.pending_priority_events():
            try:
                event = event_from_audit_record(record)
            except (KeyError, TypeError, ValueError) as exc:
                event_id = str(record.get("event_id") or "")
                status = str(record.get("status") or "")
                retry_raw = record.get("retry_count", 0)
                retry_count = retry_raw if isinstance(retry_raw, int) and not isinstance(retry_raw, bool) else 0
                if event_id and status:
                    self._event_audit.compare_and_set_event(
                        event_id,
                        expected_status=status,
                        status="failed",
                        retry_count=retry_count + 1,
                        error="invalid_persisted_event",
                    )
                self._state.record_error(f"cannot replay persisted priority event: {exc}")
                continue
            status = str(record.get("status") or "")
            if event.config_version != self._config_version:
                self._event_audit.compare_and_set_event(
                    event.event_id,
                    expected_status=status,
                    status="failed",
                    retry_count=event.retry_count,
                    error="config_version_mismatch",
                )
                self._state.record_error("cannot replay priority event from another config version")
                continue
            if status == "running" and not self._event_audit.compare_and_set_event(
                event.event_id,
                expected_status="running",
                status="pending",
                retry_count=event.retry_count,
            ):
                continue
            if self._queue.put(event):
                self._state.increment("events_replayed")
                self._queue.record_replayed()
        return {**recovery, "catchup_frozen": len(catchup)}

    def start(self) -> bool:
        with self._lifecycle_lock:
            if self._worker is not None and self._worker.is_alive():
                return False
            if self._stopped:
                raise RuntimeError("recommendation pipeline cannot restart after stop")
            started_pools: list[BoundedExecutor] = []
            try:
                self._persistence_pool.start()
                started_pools.append(self._persistence_pool)
                self._persistence_running = True
                for pool in self._compute_pools:
                    pool.start()
                    started_pools.append(pool)
                self._stop_event.clear()
                worker = threading.Thread(target=self._worker_loop, name="trader-merge", daemon=False)
                self._worker = worker
                self._accepting = True
                worker.start()
            except BaseException:
                self._accepting = False
                self._stop_event.set()
                self._queue.close()
                for pool in reversed(started_pools):
                    pool.stop()
                self._persistence_running = False
                self._stopped = True
                raise
            self._state.mark_started(True)
            return True

    def stop(self, timeout_seconds: float = 15.0) -> None:
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._accepting = False
            self._stopped = True
            self._stop_event.set()
            self._queue.close()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, timeout_seconds))
            if worker.is_alive():
                self._state.record_error("pipeline shutdown exceeded timeout while draining events")
                worker.join()
        for pool in self._compute_pools:
            pool.stop()
        self._persistence_pool.stop()
        self._persistence_running = False
        self._state.mark_started(False)

    def submit_tick(self, at: datetime | None = None) -> bool:
        with self._lifecycle_lock:
            if self._stopped or (self._worker is not None and not self._accepting):
                return False
        now = at or self._now()
        trade_day = trade_date_at(now)
        is_trading_day = self._calendar.is_trading_day(trade_day)
        decision = decision_at(now, is_trading_day=is_trading_day)
        self._state.record_tick(decision.phase.value, now)
        if decision.phase is MarketPhase.CLOSED:
            return False
        schedule_point = schedule_point_at(now, is_trading_day=is_trading_day)
        if decision.phase in {MarketPhase.DEEPSEEK_CUTOFF, MarketPhase.FINAL_QUOTE, MarketPhase.AFTER_CLOSE} and (
            schedule_point is None
        ):
            return False
        is_freeze = bool(decision.freeze_strategies)
        event_type = "freeze" if is_freeze else "market_quotes"
        priority = EventPriority.FREEZE if is_freeze else EventPriority.MARKET_QUOTES
        if schedule_point is SchedulePoint.DEEPSEEK_CUTOFF:
            event_type = schedule_point.value
            priority = EventPriority.DEEPSEEK
        elif schedule_point in {SchedulePoint.FINAL_CANDIDATE_QUOTES, SchedulePoint.CLOSE_QUOTES}:
            event_type = schedule_point.value
            priority = EventPriority.CANDIDATE_QUOTES
        event = new_event(
            event_type,
            subject_key="market",
            trade_date=trade_day.isoformat(),
            phase=decision.phase.value,
            strategy=None,
            priority=priority,
            data_version=(
                f"schedule:{schedule_point.value}"
                if schedule_point is not None
                else f"tick:{shanghai_now(now).strftime('%H%M%S')}"
            ),
            config_version=self._config_version,
            created_at=now,
            payload={
                "freeze_strategies": list(decision.freeze_strategies),
                "schedule_point": schedule_point.value if schedule_point is not None else "",
            },
        )
        return self.submit_event(event)

    def submit_due(self, at: datetime | None = None) -> float:
        now = at or self._now()
        trade_day = trade_date_at(now)
        is_trading_day = self._calendar.is_trading_day(trade_day)
        planner = self._cadence
        if planner is None:
            self.submit_tick(now)
            return 1.0
        batch = planner.plan(now, is_trading_day=is_trading_day)
        phase = decision_at(now, is_trading_day=is_trading_day).phase
        self._state.record_tick(phase.value, now)
        for task in batch.tasks:
            self._state.increment(f"cadence_{task.task.value}_planned")
            if not self._candidate_codes and task.task not in {
                PipelineTask.FULL_MARKET,
                PipelineTask.REFERENCE_DATA,
                PipelineTask.FREEZE,
                PipelineTask.DEEPSEEK_CUTOFF,
                PipelineTask.CLOSE_QUOTES,
            }:
                self._state.increment(f"cadence_{task.task.value}_skipped_cold")
                continue
            self._submit_scheduled_task(task)
        return batch.next_delay_seconds

    def _submit_scheduled_task(self, scheduled: ScheduledPipelineTask) -> bool:
        task = scheduled.task
        with self._cadence_lock:
            if task in self._scheduled_inflight:
                self._state.increment("cadence_skipped_inflight")
                self._state.increment(f"cadence_{task.value}_skipped_inflight")
                return False
            self._scheduled_inflight.add(task)
        is_freeze = task is PipelineTask.FREEZE
        priority = {
            PipelineTask.FREEZE: EventPriority.FREEZE,
            PipelineTask.STOCK_RISK: EventPriority.RISK,
            PipelineTask.DEEPSEEK_CUTOFF: EventPriority.DEEPSEEK,
            PipelineTask.SCORE: EventPriority.SCORE,
            PipelineTask.CANDIDATE_QUOTES: EventPriority.CANDIDATE_QUOTES,
            PipelineTask.FINAL_CANDIDATE_QUOTES: EventPriority.CANDIDATE_QUOTES,
            PipelineTask.TOPK_QUOTES: EventPriority.CANDIDATE_QUOTES,
            PipelineTask.CLOSE_QUOTES: EventPriority.CANDIDATE_QUOTES,
            PipelineTask.FULL_MARKET: EventPriority.MARKET_QUOTES,
            PipelineTask.INDUSTRY_HEAT: EventPriority.MARKET_QUOTES,
            PipelineTask.MARKET_NEWS: EventPriority.MARKET_QUOTES,
            PipelineTask.REFERENCE_DATA: EventPriority.LONG,
        }[task]
        local = shanghai_now(scheduled.scheduled_at)
        event = new_event(
            "freeze" if is_freeze else task.value,
            subject_key="market",
            trade_date=local.date().isoformat(),
            phase=scheduled.phase.value,
            strategy=None,
            priority=priority,
            data_version=f"cadence:{task.value}:{local.strftime('%H%M%S')}",
            config_version=self._config_version,
            created_at=scheduled.scheduled_at,
            deadline=_scheduled_task_deadline(scheduled),
            payload={
                "freeze_strategies": list(scheduled.freeze_strategies),
                "schedule_task": task.value,
            },
        )
        accepted = self.submit_event(event)
        if not accepted:
            with self._cadence_lock:
                self._scheduled_inflight.discard(task)
        else:
            self._state.increment(f"cadence_{task.value}_submitted")
        return accepted

    def submit_event(self, event: PipelineEvent) -> bool:
        with self._lifecycle_lock:
            if self._stopped or (self._worker is not None and not self._accepting):
                return False
        if event.config_version != self._config_version:
            self._state.record_error("event config version does not match the active runtime")
            return False
        is_priority = event.priority <= EventPriority.RISK
        if is_priority:
            try:
                if not persist(self, self._event_audit.reserve_event, event.audit_record(status="pending")):
                    self._state.increment("event_reservation_conflicts")
                    return False
            except Exception as exc:
                self._state.record_error(f"cannot persist priority event: {str(exc)[:500]}")
                return False
        accepted = self._queue.put(event)
        if accepted:
            self._state.increment("events_submitted")
        elif is_priority:
            self._state.record_error("priority queue full; event retained for restart replay")
        return accepted

    def run_once(self, at: datetime) -> tuple[RecommendationSnapshot, ...]:
        trade_day = trade_date_at(at)
        is_trading_day = self._calendar.is_trading_day(trade_day)
        decision = decision_at(at, is_trading_day=is_trading_day)
        self._state.record_tick(decision.phase.value, at)
        if decision.phase is MarketPhase.CLOSED:
            return ()
        snapshots = process_schedule(self, at, decision.phase, decision.freeze_strategies)
        self._record_health_snapshot()
        return snapshots

    def status(self) -> dict[str, object]:
        measured_at = self._now()
        market_data = dict(self._market_data.health())
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
            "worker_pools": worker_status(self),
            "cadence": cadence_status,
            "publisher": self._publisher.status(),
            "persistent_audit": self._observability_status(),
        }
        return self._state.snapshot(dependencies)

    def _observability_status(self) -> Mapping[str, object]:
        provider = getattr(self._repository, "observability_status", None)
        if not callable(provider):
            return {}
        try:
            return dict(provider())
        except (OSError, RuntimeError, ValueError):
            return {"error": "persistent_observability_unavailable"}

    def _record_health_snapshot(self) -> None:
        recorder = getattr(self._repository, "record_data_source_health", None)
        if not callable(recorder):
            return
        health = dict(self._market_data.health())
        updated_at = self._now()
        if not self._persistence_running:
            recorder(health, updated_at=updated_at)
            return
        future = self._persistence_pool.submit(recorder, health, updated_at=updated_at)
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

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            event = self._queue.get(timeout_seconds=0.5)
            if event is None:
                continue
            with self._merge_status_lock:
                self._merge_inflight += 1
                self._merge_submitted_count += 1
            try:
                if event.priority > EventPriority.RISK and not persist(
                    self,
                    self._event_audit.reserve_event,
                    event.audit_record(status="pending"),
                ):
                    self._state.increment("event_reservation_conflicts")
                    continue
                if not persist(
                    self,
                    self._event_audit.compare_and_set_event,
                    event.event_id,
                    expected_status="pending",
                    status="running",
                    retry_count=event.retry_count,
                ):
                    self._state.increment("event_claim_conflicts")
                    continue
                if (
                    event.deadline is not None
                    and event.priority is not EventPriority.FREEZE
                    and self._now() >= event.deadline
                ):
                    raise RuntimeError(f"event deadline expired before execution: {event.event_type}")
                process_event_on_workers(self, event)
                if (
                    event.deadline is not None
                    and event.priority is not EventPriority.FREEZE
                    and self._now() >= event.deadline
                ):
                    raise RuntimeError(f"event deadline expired during execution: {event.event_type}")
                if not persist(
                    self,
                    self._event_audit.compare_and_set_event,
                    event.event_id,
                    expected_status="running",
                    status="success",
                    retry_count=event.retry_count,
                ):
                    raise RuntimeError(f"event terminal compare-and-set failed: {event.event_id}")
                self._state.increment("events_completed")
            except Exception as exc:
                _LOGGER.exception("pipeline event failed", extra={"event_id": event.event_id})
                try:
                    persist(
                        self,
                        self._event_audit.compare_and_set_event,
                        event.event_id,
                        expected_status="running",
                        status="failed",
                        retry_count=event.retry_count,
                        error=str(exc),
                    )
                except Exception:
                    _LOGGER.exception("pipeline event failure state could not be persisted")
                self._state.increment("events_failed")
                self._state.record_error(str(exc))
            finally:
                self._record_health_snapshot()
                task_raw = event.payload.get("schedule_task")
                if isinstance(task_raw, str):
                    try:
                        scheduled_task = PipelineTask(task_raw)
                    except ValueError:
                        pass
                    else:
                        with self._cadence_lock:
                            self._scheduled_inflight.discard(scheduled_task)
                with self._merge_status_lock:
                    self._merge_inflight -= 1
                    self._merge_completed_count += 1

    def _freeze_available_snapshots(
        self,
        now: datetime,
        freeze_strategies: Sequence[str],
    ) -> tuple[RecommendationSnapshot, ...]:
        return freeze_available_snapshots(self, now, freeze_strategies)

    def _refresh_live_overlays(
        self,
        now: datetime,
        phase: MarketPhase,
        *,
        deadline: datetime | None = None,
    ) -> None:
        refresh_live_overlays(self, now, phase, deadline=deadline)


def _scheduled_task_deadline(scheduled: ScheduledPipelineTask) -> datetime | None:
    seconds = {
        PipelineTask.FULL_MARKET: 20.0,
        PipelineTask.CANDIDATE_QUOTES: 3.0,
        PipelineTask.TOPK_QUOTES: 3.0,
        PipelineTask.SCORE: 15.0,
        PipelineTask.INDUSTRY_HEAT: 20.0,
        PipelineTask.MARKET_NEWS: 8.0,
        PipelineTask.STOCK_RISK: 8.0,
        PipelineTask.REFERENCE_DATA: 20.0,
        PipelineTask.DEEPSEEK_CUTOFF: 1.0,
        PipelineTask.FINAL_CANDIDATE_QUOTES: 10.0,
        PipelineTask.CLOSE_QUOTES: 3.0,
        PipelineTask.FREEZE: None,
    }[scheduled.task]
    return scheduled.scheduled_at + timedelta(seconds=seconds) if seconds is not None else None


_CRITICAL_TOPK_PHASES = {
    MarketPhase.TODAY_OBSERVE,
    MarketPhase.TODAY_MAIN,
    MarketPhase.FINAL_REVIEW,
    MarketPhase.DEEPSEEK_CUTOFF,
    MarketPhase.FINAL_QUOTE,
}


__all__ = ["RecommendationPipeline"]
