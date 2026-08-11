"""Bounded recommendation pipeline and deterministic single-tick use case."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from datetime import datetime, timedelta
from uuid import uuid4

from trader.application.cadence import (
    CadencePlanner,
    PipelineTask,
    SchedulePointResult,
)
from trader.application.events import (
    BoundedEventQueue,
    EventDeadlineExpiredError,
    EventPriority,
    EventStatus,
    PipelineEvent,
)
from trader.application.latency import LatencyWaterfall
from trader.application.long_quotes import LongQuoteProjectionService, refresh_long_quotes
from trader.application.pipeline_dependencies import PipelineDependencies, PipelineOptions, PipelineResources
from trader.application.pipeline_research import PipelineResearchMixin
from trader.application.pipeline_stages import process_event_on_workers
from trader.application.pipeline_status import PipelineStatusMixin
from trader.application.pipeline_submission import PipelineSubmissionMixin
from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.research_coordination import ResearchCoordinator
from trader.application.schedule import (
    MarketPhase,
    SchedulePoint,
    decision_at,
    shanghai_now,
    startup_freeze_strategies,
    trade_date_at,
)
from trader.application.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.application.snapshot_publication import admit_snapshot_to_p6
from trader.application.snapshot_workflow import (
    freeze_available_snapshots,
    process_schedule,
    refresh_live_overlays,
)
from trader.application.source_lanes import LatestRequestLane, SourceRequestSupersededError
from trader.application.trading_session import TradingSessionStatus, TradingSessionTracker
from trader.application.workers import BoundedExecutor
from trader.domain.market.models import FeatureSnapshot
from trader.domain.recommendation.models import (
    FilterAudit,
    LiveOverlay,
    RecommendationSnapshot,
    Strategy,
)

_LOGGER = logging.getLogger(__name__)


class RecommendationPipeline(PipelineSubmissionMixin, PipelineResearchMixin, PipelineStatusMixin):
    def __init__(
        self,
        dependencies: PipelineDependencies,
        options: PipelineOptions,
        resources: PipelineResources,
    ) -> None:
        self._bind_dependencies(dependencies, options)
        self._configure_workers(options, resources)
        self._initialize_runtime_state()

    def _bind_dependencies(
        self,
        dependencies: PipelineDependencies,
        options: PipelineOptions,
    ) -> None:
        self._market_full = dependencies.market.full_market
        self._candidate_data = dependencies.market.candidates
        self._quotes = dependencies.market.quotes
        self._research = dependencies.market.research
        self._references = dependencies.market.references
        self._market_metadata = dependencies.market.metadata
        self._calendar = dependencies.calendar
        self._reviews = dependencies.reviews
        self._repository = dependencies.snapshots.reader
        self._snapshot_writer = dependencies.snapshots.writer
        self._event_audit = dependencies.events
        self._publisher = dependencies.publisher
        self._published_snapshots = dependencies.published_snapshots or dependencies.state
        self._engine = dependencies.engine
        self._state = dependencies.state
        self._config_version = options.config_version
        self._options_decision_execution_mode = options.decision_execution_mode
        self._candidate_pool_size = options.candidate_pool_size
        self._now = dependencies.now
        self._trading_session = dependencies.trading_session or TradingSessionTracker(self._now())
        self._trading_session.add_rotation_hook(self._handle_session_rotation)
        self._long_projection = LongQuoteProjectionService(
            codes=options.long_codes,
            items=options.long_items,
            groups=options.long_groups,
        )
        self._outcome_settlement = dependencies.outcome_settlement
        self._tomorrow_native_inputs = dependencies.tomorrow_native_inputs
        self._today_native_inputs = dependencies.today_native_inputs
        self._tomorrow_v2_control = dependencies.tomorrow_v2_control
        self._v2_controls = dependencies.v2_controls or (
            (dependencies.tomorrow_v2_control,) if dependencies.tomorrow_v2_control is not None else ()
        )
        self._v2_overlays = dependencies.v2_overlays
        self._v2_owned_strategies = frozenset(options.v2_owned_strategies)
        self._latency = dependencies.latency or LatencyWaterfall()
        self._market_data_manages_workers = options.market_data_manages_workers
        self._cadence = (
            CadencePlanner(options.cadence_policy, started_at=self._now())
            if options.cadence_policy is not None
            else None
        )

    def _configure_workers(
        self,
        options: PipelineOptions,
        resources: PipelineResources,
    ) -> None:
        self._queue = BoundedEventQueue(
            maximum_size=options.event_queue_size,
            reserved_priority_size=options.priority_queue_size,
        )
        worker_queue_capacity = max(1, options.event_queue_size)
        self._data_pool = resources.data_pool or BoundedExecutor(
            worker_count=options.market_workers,
            urgent_worker_count=1 if options.market_workers > 1 else 0,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-data",
        )
        self._normalization_pool = BoundedExecutor(
            worker_count=options.normalization_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-normalize",
        )
        self._strategy_pool = BoundedExecutor(
            worker_count=options.strategy_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-strategy",
        )
        self._deepseek_pool = BoundedExecutor(
            worker_count=options.deepseek_workers,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-deepseek",
        )
        self._long_quote_pool = BoundedExecutor(
            worker_count=1,
            queue_capacity=1,
            thread_name_prefix="trader-long-quotes",
        )
        self._long_quote_lane = LatestRequestLane(
            "long-quotes",
            self._long_quote_pool,
            latency=self._latency,
            latency_stage="long_quote_queue_wait",
        )
        self._overlay_pool = BoundedExecutor(
            worker_count=1,
            queue_capacity=1,
            thread_name_prefix="trader-overlay",
        )
        self._overlay_lane = LatestRequestLane(
            "topk-overlay",
            self._overlay_pool,
            latency=self._latency,
            latency_stage="overlay_queue_wait",
        )
        self._persistence_pool = resources.persistence_pool or BoundedExecutor(
            worker_count=1,
            queue_capacity=worker_queue_capacity,
            thread_name_prefix="trader-persistence",
        )
        self._compute_pools = (
            self._data_pool,
            self._normalization_pool,
            self._strategy_pool,
            self._deepseek_pool,
            self._long_quote_pool,
            self._overlay_pool,
        )
        self._research_coordinator = ResearchCoordinator(
            self._research,
            now=self._now,
            on_result=self._on_company_research_result,
        )

    def _initialize_runtime_state(self) -> None:
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
        self._session_snapshot_ids: set[str] = set()
        self._company_research_membership_lock = threading.Lock()
        self._company_research_membership_date = ""
        self._company_research_membership: set[str] = set()
        self._company_research_review_barrier = False
        self._company_research_initial_rescore_pending = False
        self._after_close_lock = threading.Lock()
        self._outcome_settlement_lock = threading.Lock()
        self._pending_hybrid_lock = threading.Lock()
        self._pending_hybrids: dict[str, object] = {}
        self._async_review_lock = threading.Lock()
        self._async_review_inflight: set[Strategy] = set()
        self._async_review_pending: dict[Strategy, object] = {}
        self._after_close_retry_at: datetime | None = None
        self._after_close_retry_attempt = 0
        self._after_close_completed_date = ""
        self._outcome_settlement_date = ""
        self._decision_execution_mode = self._options_decision_execution_mode
        self._overlay_lock = threading.Lock()

    def _settle_outcomes(self, now: datetime) -> None:
        if self._outcome_settlement is None:
            return
        trade_date = trade_date_at(now).isoformat()
        with self._outcome_settlement_lock:
            if self._outcome_settlement_date == trade_date:
                return
            try:
                result = self._outcome_settlement.settle(now, self._market_features)
            except SourceRequestSupersededError:
                self._state.increment("outcome_settlement_superseded")
                return
            except Exception as exc:
                self._state.increment("outcome_settlement_failures")
                self._state.record_error(f"outcome settlement degraded: {type(exc).__name__}")
                return
            if result.benchmark_recorded:
                self._outcome_settlement_date = trade_date
            self._state.increment("outcome_settlement_runs")
            self._state.increment("outcome_settlement_completed", result.completed_count)

    def initialize(self) -> Mapping[str, int]:
        self._snapshot_writer.initialize()
        recovery = self._snapshot_writer.recover()
        retention = getattr(self._snapshot_writer, "enforce_retention", None)
        archived = int(retention()) if callable(retention) else 0
        self._restore_frozen_snapshots()
        catchup = self._catch_up_due_freezes()
        return {
            "recovered": recovery.recovered,
            "quarantined": recovery.quarantined,
            "orphaned": recovery.orphaned,
            "archived": archived,
            "catchup_frozen": len(catchup),
        }

    def _restore_frozen_snapshots(self) -> None:
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            for trade_date in self._repository.recommendation_dates(strategy):
                self._frozen_keys.add((strategy, trade_date))
        for strategy in (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25):
            dates = self._repository.recommendation_dates(strategy)
            latest = self._repository.load_frozen(strategy, dates[0]) if dates else None
            if latest is not None and admit_snapshot_to_p6(self, latest):
                self._state.restore_snapshot(latest)
                self._state.restore_frozen(strategy, latest.trade_date)
                overlay = self._repository.load_live_overlay(strategy, latest.trade_date)
                if overlay is not None and overlay.snapshot_id == latest.snapshot_id:
                    self._live_overlays[(strategy, latest.trade_date)] = overlay
                    self._state.restore_overlay(overlay)

    def _catch_up_due_freezes(self) -> tuple[RecommendationSnapshot, ...]:
        now = self._now()
        trade_day = trade_date_at(now)
        trade_day_iso = trade_day.isoformat()
        session = self._refresh_trading_session(now)
        freeze_targets = startup_freeze_strategies(now, is_trading_day=session.is_trading_day is True)
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
        return self._freeze_available_snapshots(now, freeze_targets)

    def session_status(self) -> TradingSessionStatus:
        return self._trading_session.status()

    def observe_clock(
        self,
        wall_at: datetime,
        *,
        monotonic_seconds: float,
        planned_interval_seconds: float,
    ) -> TradingSessionStatus:
        return self._trading_session.observe_clock(
            wall_at,
            monotonic_seconds=monotonic_seconds,
            planned_interval_seconds=planned_interval_seconds,
        )

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
                self._research_coordinator.start()
                self._stop_event.clear()
                worker = threading.Thread(target=self._worker_loop, name="trader-merge", daemon=False)
                self._worker = worker
                self._accepting = True
                worker.start()
            except BaseException:
                self._research_coordinator.stop(wait=False)
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

    def stop(
        self,
        timeout_seconds: float = 15.0,
        *,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownReport:
        deadline = deadline or ShutdownDeadline.start(timeout_seconds)
        with self._lifecycle_lock:
            if self._stopped:
                return ShutdownReport.from_steps(deadline, ())
            self._accepting = False
            self._stopped = True
            self._stop_event.set()
            queue_result = self._queue.close(ledger=self._event_audit)
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(deadline.remaining_seconds())
        merge_completed = worker is None or not worker.is_alive()
        if not merge_completed:
            self._state.record_error("pipeline shutdown exceeded deadline while draining priority events")
        steps = [
            ShutdownStep(
                name="event-queue",
                completed=True,
                timed_out=False,
                cancelled_count=len(queue_result.cancelled_event_ids),
            ),
            ShutdownStep(
                name="merge",
                completed=merge_completed,
                timed_out=not merge_completed and deadline.expired,
                detail="priority events remain inflight" if not merge_completed else "",
            ),
            self._overlay_lane.stop(wait=True, deadline=deadline),
            self._long_quote_lane.stop(wait=True, deadline=deadline),
            self._research_coordinator.stop(wait=True, deadline=deadline),
        ]
        for pool in self._compute_pools:
            steps.append(pool.stop(wait=True, cancel_futures=True, deadline=deadline))
        steps.extend(self._engine.stop(deadline=deadline))
        steps.append(self._persistence_pool.stop(wait=True, cancel_futures=True, deadline=deadline))
        self._persistence_running = False
        self._state.mark_started(False)
        return ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)

    def run_once(self, at: datetime) -> tuple[RecommendationSnapshot, ...]:
        correlation_id = f"run-once:{uuid4().hex}"
        self._latency.plan(correlation_id, "run_once")
        self._latency.enter(correlation_id)
        try:
            session = self._refresh_trading_session(at)
            is_trading_day = session.is_trading_day is True
            decision = decision_at(at, is_trading_day=is_trading_day)
            self._state.record_tick(decision.phase.value, at)
            if decision.phase is MarketPhase.CLOSED:
                self._latency.finish(correlation_id, outcome="success")
                return ()
            snapshots = process_schedule(self, at, decision.phase, decision.freeze_strategies)
        except BaseException:
            self._latency.finish(correlation_id, outcome="failed")
            raise
        self._latency.finish(correlation_id, outcome="success")
        return snapshots

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set() or not self._queue.empty():
            event = self._queue.get(timeout_seconds=0.5)
            if event is None:
                continue
            self._begin_event()
            try:
                self._process_event(event)
            except (EventDeadlineExpiredError, MarketDataDeadlineExceededError) as exc:
                self._expire_event(event, exc)
            except Exception as exc:
                self._fail_event(event, exc)
            finally:
                self._finish_event(event)

    def _begin_event(self) -> None:
        with self._merge_status_lock:
            self._merge_inflight += 1
            self._merge_submitted_count += 1

    def _process_event(self, event: PipelineEvent) -> None:
        self._latency.enter(event.event_id)
        if not self._event_matches_session(event):
            self._reject_stale_session_event(event)
            return
        if event.priority > EventPriority.RISK and not self._event_audit.reserve_event(
            event.audit_record(status=EventStatus.PENDING)
        ):
            self._state.increment("event_reservation_conflicts")
            self._latency.finish(event.event_id, outcome="dropped")
            return
        if not self._event_audit.compare_and_set_event(
            event.event_id,
            expected_status=EventStatus.PENDING,
            status=EventStatus.RUNNING,
            retry_count=event.retry_count,
        ):
            self._state.increment("event_claim_conflicts")
            self._latency.finish(event.event_id, outcome="dropped")
            return
        self._ensure_event_deadline(event, "before")
        process_event_on_workers(self, event)
        self._ensure_event_deadline(event, "during")
        self._record_schedule_result(event, SchedulePointResult.COMPLETED)
        if not self._event_audit.compare_and_set_event(
            event.event_id,
            expected_status=EventStatus.RUNNING,
            status=EventStatus.SUCCESS,
            retry_count=event.retry_count,
        ):
            raise RuntimeError(f"event terminal compare-and-set failed: {event.event_id}")
        self._state.increment("events_completed")
        self._latency.finish(event.event_id, outcome="success")

    def _ensure_event_deadline(self, event: PipelineEvent, stage: str) -> None:
        if event.deadline is not None and event.priority is not EventPriority.FREEZE and self._now() >= event.deadline:
            raise EventDeadlineExpiredError(f"event deadline expired {stage} execution: {event.event_type}")

    def _expire_event(
        self,
        event: PipelineEvent,
        error: EventDeadlineExpiredError | MarketDataDeadlineExceededError,
    ) -> None:
        _LOGGER.info("pipeline event expired", extra={"event_id": event.event_id})
        try:
            self._event_audit.compare_and_set_event(
                event.event_id,
                expected_status=EventStatus.RUNNING,
                status=EventStatus.EXPIRED,
                retry_count=event.retry_count,
                error=str(error),
            )
        except Exception:
            _LOGGER.exception("pipeline event expiration state could not be recorded in memory")
        self._state.increment("events_expired")
        self._record_schedule_result(event, SchedulePointResult.RETRY)
        self._latency.finish(event.event_id, outcome="timeout")

    def _fail_event(self, event: PipelineEvent, error: Exception) -> None:
        _LOGGER.exception("pipeline event failed", extra={"event_id": event.event_id})
        try:
            self._event_audit.compare_and_set_event(
                event.event_id,
                expected_status=EventStatus.RUNNING,
                status=EventStatus.FAILED,
                retry_count=event.retry_count,
                error=str(error),
            )
        except Exception:
            _LOGGER.exception("pipeline event failure state could not be recorded in memory")
        self._state.increment("events_failed")
        self._state.record_error(str(error))
        self._record_schedule_result(event, SchedulePointResult.RETRY)
        self._latency.finish(event.event_id, outcome="failed")

    def _event_matches_session(self, event: PipelineEvent) -> bool:
        generation = event.payload.get("session_generation")
        trade_date = event.payload.get("session_trade_date")
        if not isinstance(generation, int) or isinstance(generation, bool) or not isinstance(trade_date, str):
            return True
        return self._trading_session.accepts(generation, trade_date)

    def _reject_stale_session_event(self, event: PipelineEvent) -> None:
        if event.priority <= EventPriority.RISK:
            self._event_audit.compare_and_set_event(
                event.event_id,
                expected_status=EventStatus.PENDING,
                status=EventStatus.FAILED,
                retry_count=event.retry_count,
                error="stale_session_generation",
            )
        self._state.increment("events_stale_session_rejected")
        self._record_schedule_result(event, SchedulePointResult.RETRY)
        self._latency.finish(event.event_id, outcome="superseded")

    def _record_schedule_result(self, event: PipelineEvent, result: SchedulePointResult) -> None:
        planner = self._cadence
        raw_point = event.payload.get("schedule_point")
        if planner is None or not isinstance(raw_point, str) or not raw_point:
            return
        try:
            point = SchedulePoint(raw_point)
        except ValueError:
            return
        if point in {SchedulePoint.TODAY_FREEZE, SchedulePoint.AFTERNOON_FREEZE}:
            if result is SchedulePointResult.COMPLETED:
                return
            raw_strategies = event.payload.get("freeze_strategies")
            strategies = tuple(str(value) for value in raw_strategies) if isinstance(raw_strategies, tuple) else ()
            for strategy in strategies:
                planner.record_point_result(event.trade_date, point, strategy, result, at=self._now())
            return
        strategy = "today" if point is SchedulePoint.TODAY_CHECKPOINT else "-"
        planner.record_point_result(event.trade_date, point, strategy, result, at=self._now())

    def _finish_event(self, event: PipelineEvent) -> None:
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
        legacy_strategies = tuple(
            value for value in freeze_strategies if Strategy(value) not in self._v2_owned_strategies
        )
        return freeze_available_snapshots(self, now, legacy_strategies)

    def _refresh_live_overlays(
        self,
        now: datetime,
        phase: MarketPhase,
        *,
        deadline: datetime | None = None,
    ) -> None:
        with self._overlay_lock:
            refresh_live_overlays(self, now, phase, deadline=deadline)

    def _submit_overlay_event(self, event: PipelineEvent) -> bool:
        self._latency.plan(event.event_id, event.event_type)
        future = self._overlay_lane.submit(
            event.event_id,
            event.created_at,
            self._execute_overlay_event,
            event,
        )
        future.add_done_callback(lambda completed: self._finish_overlay_event(event, completed))
        if future.done():
            try:
                future.result()
            except SourceRequestSupersededError:
                return True
            except RuntimeError:
                return False
        self._state.increment("overlay_events_submitted")
        return True

    def _submit_long_quote_event(self, event: PipelineEvent) -> bool:
        self._latency.plan(event.event_id, event.event_type)
        future = self._long_quote_lane.submit(
            "long-watchlist",
            event.created_at,
            self._execute_long_quote_event,
            event,
        )
        future.add_done_callback(lambda completed: self._finish_long_quote_event(event, completed))
        if future.done():
            try:
                future.result()
            except SourceRequestSupersededError:
                return True
            except RuntimeError:
                return False
        self._state.increment("long_quote_events_submitted")
        return True

    def _execute_long_quote_event(self, event: PipelineEvent) -> None:
        self._latency.enter(event.event_id)
        refresh_long_quotes(
            self,
            max(event.created_at, self._now()),
            MarketPhase(event.phase),
            deadline=event.deadline,
        )

    def _finish_long_quote_event(self, event: PipelineEvent, future: Future[None]) -> None:
        try:
            future.result()
        except SourceRequestSupersededError:
            self._state.increment("long_quote_events_superseded")
            self._latency.finish(event.event_id, outcome="superseded")
        except Exception as exc:
            self._state.increment("long_quote_events_failed")
            self._state.record_error(f"long 行情刷新暂时降级：{type(exc).__name__}")
            self._latency.finish(event.event_id, outcome="failed")
        else:
            self._state.increment("long_quote_events_completed")
            self._latency.finish(event.event_id, outcome="success")

    def _execute_overlay_event(self, event: PipelineEvent) -> None:
        self._latency.enter(event.event_id)
        self._refresh_live_overlays(
            max(event.created_at, self._now()),
            MarketPhase(event.phase),
            deadline=event.deadline,
        )

    def _finish_overlay_event(self, event: PipelineEvent, future: Future[None]) -> None:
        try:
            future.result()
        except SourceRequestSupersededError:
            self._state.increment("overlay_events_superseded")
            self._latency.finish(event.event_id, outcome="superseded")
        except Exception as exc:
            self._state.increment("overlay_events_failed")
            self._state.record_error(f"TopK 行情刷新暂时降级：{type(exc).__name__}")
            self._latency.finish(event.event_id, outcome="failed")
        else:
            self._state.increment("overlay_events_completed")
            self._latency.finish(event.event_id, outcome="success")

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

        snapshot = self._state.latest(strategy)
        if snapshot is None or snapshot.trade_date != trade_date:
            snapshot = self._snapshot_writer.load_checkpoint(strategy, trade_date, boundary_at=boundary)
            if snapshot is None:
                return False

        if snapshot.published_at > boundary:
            return False
        age_seconds = (boundary - snapshot.published_at).total_seconds()
        if age_seconds > cutoff_seconds:
            return False
        return True

    def _record_after_close_recovery(self, now: datetime, *, complete: bool) -> None:
        trade_date = trade_date_at(now).isoformat()
        if complete:
            self._after_close_completed_date = trade_date
            self._after_close_retry_at = None
            self._after_close_retry_attempt = 0
            return
        delays = (3.0, 5.0, 10.0, 20.0, 30.0)
        index = min(self._after_close_retry_attempt, len(delays) - 1)
        self._after_close_retry_at = now + timedelta(seconds=delays[index])
        self._after_close_retry_attempt += 1


__all__ = ["RecommendationPipeline"]
