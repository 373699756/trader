"""Bounded recommendation pipeline and deterministic single-tick use case."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from trader.application.cadence import (
    CadencePlanner,
    CadencePolicy,
    PipelineTask,
)
from trader.application.events import (
    BoundedEventQueue,
    EventDeadlineExpired,
    EventPriority,
    event_from_audit_record,
)
from trader.application.pipeline_stages import (
    persist,
    process_event_on_workers,
)
from trader.application.pipeline_status import PipelineStatusMixin
from trader.application.pipeline_submission import PipelineSubmissionMixin
from trader.application.ports import (
    DeepSeekReviewPort,
    EventAuditPort,
    MarketDataDeadlineExceeded,
    MarketDataPort,
    SnapshotRepositoryPort,
    TradingCalendarPort,
)
from trader.application.publisher import SnapshotPublisher
from trader.application.recommendations import RecommendationEngine
from trader.application.schedule import (
    MarketPhase,
    decision_at,
    freeze_due_at,
    shanghai_now,
    trade_date_at,
)
from trader.application.snapshot_workflow import (
    freeze_available_snapshots,
    process_schedule,
    refresh_live_overlays,
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


class RecommendationPipeline(PipelineSubmissionMixin, PipelineStatusMixin):
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
        persistence_pool: BoundedExecutor | None = None,
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
            urgent_worker_count=1 if market_workers > 1 else 0,
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
        self._persistence_pool = persistence_pool or BoundedExecutor(
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
                    self._state.restore_overlay(overlay)
        now = self._now()
        trade_day = trade_date_at(now)
        trade_day_iso = trade_day.isoformat()
        freeze_targets = freeze_due_at(now, is_trading_day=self._calendar.is_trading_day(trade_day))
        if freeze_targets:
            freeze_targets = tuple(
                target
                for target in freeze_targets
                if self._has_pre_cutoff_snapshot_for_catchup(
                    Strategy(target),
                    now=now,
                    trade_date=trade_day_iso,
                )
            )
        catchup = self._freeze_available_snapshots(now, freeze_targets)
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
            engine_started = False
            try:
                self._engine.start()
                engine_started = True
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
                if engine_started:
                    self._engine.stop()
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
        self._engine.stop()
        self._persistence_pool.stop()
        self._persistence_running = False
        self._state.mark_started(False)

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
                    raise EventDeadlineExpired(f"event deadline expired before execution: {event.event_type}")
                process_event_on_workers(self, event)
                if (
                    event.deadline is not None
                    and event.priority is not EventPriority.FREEZE
                    and self._now() >= event.deadline
                ):
                    raise EventDeadlineExpired(f"event deadline expired during execution: {event.event_type}")
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
            except (EventDeadlineExpired, MarketDataDeadlineExceeded) as exc:
                _LOGGER.info("pipeline event expired", extra={"event_id": event.event_id})
                try:
                    persist(
                        self,
                        self._event_audit.compare_and_set_event,
                        event.event_id,
                        expected_status="running",
                        status="expired",
                        retry_count=event.retry_count,
                        error=str(exc),
                    )
                except Exception:
                    _LOGGER.exception("pipeline event expiration state could not be persisted")
                self._state.increment("events_expired")
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

    def _has_pre_cutoff_snapshot_for_catchup(
        self,
        strategy: Strategy,
        now: datetime,
        trade_date: str,
    ) -> bool:
        local = shanghai_now(now)
        boundary = (
            local.replace(hour=11, minute=20, second=0, microsecond=0)
            if strategy is Strategy.TODAY
            else local.replace(hour=14, minute=50, second=0, microsecond=0)
        )
        cutoff_seconds = 20.0 if strategy is Strategy.TODAY else 30.0

        latest = self._state.latest(strategy)
        if latest is not None and latest.trade_date == trade_date:
            snapshot = latest
        else:
            fallback = self._repository.latest(strategy)
            if fallback is None or fallback.trade_date != trade_date:
                return False
            snapshot = fallback

        if snapshot.published_at > boundary:
            return False
        age_seconds = (boundary - snapshot.published_at).total_seconds()
        if age_seconds > cutoff_seconds:
            return False
        return True


__all__ = ["RecommendationPipeline"]
