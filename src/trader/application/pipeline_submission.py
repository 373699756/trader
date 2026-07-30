"""Event submission and cadence scheduling mixin for the recommendation pipeline."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timedelta

from trader.application.cadence import PipelineTask, ScheduledPipelineTask, task_execution_budget_seconds
from trader.application.events import EventPriority, EventSpec, EventStatus, PipelineEvent, new_event
from trader.application.pipeline_state import PipelineState
from trader.application.schedule import (
    MarketPhase,
    SchedulePoint,
    decision_at,
    schedule_point_at,
    shanghai_now,
    trade_date_at,
)
from trader.application.trading_session import TradingSessionStatus, TradingSessionTracker
from trader.domain.recommendation.models import Strategy


class PipelineSubmissionMixin(PipelineState):
    _candidate_features: tuple[object, ...]
    _filter_reasons: Mapping[str, int]
    _filter_details: tuple[object, ...]
    _filtered_count: int
    _pending_hybrid_lock: threading.Lock
    _pending_hybrids: dict[str, object]
    _async_review_lock: threading.Lock
    _async_review_pending: dict[Strategy, object]

    def submit_tick(self, at: datetime | None = None) -> bool:
        with self._lifecycle_lock:
            if self._stopped or (self._worker is not None and not self._accepting):
                return False
        now = at or self._now()
        session = self._refresh_trading_session(now)
        trade_day = trade_date_at(now)
        is_trading_day = session.is_trading_day is True
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
            EventSpec(
                event_type=event_type,
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
                    "session_generation": session.generation,
                    "session_trade_date": session.trade_date,
                },
            )
        )
        return self.submit_event(event)

    def submit_due(self, at: datetime | None = None) -> float:
        now = at or self._now()
        session = self._refresh_trading_session(now)
        trade_day = trade_date_at(now)
        is_trading_day = session.is_trading_day is True
        planner = self._cadence
        if planner is None:
            if is_trading_day:
                self._offer_company_research(now)
            self.submit_tick(now)
            return 1.0
        phase = decision_at(now, is_trading_day=is_trading_day).phase
        self._state.record_tick(phase.value, now)
        batch = planner.plan(now, is_trading_day=is_trading_day)
        tasks = list(batch.tasks)
        trade_date = trade_day.isoformat()
        retry_due = self._after_close_retry_at is None or now >= self._after_close_retry_at
        if (
            phase is MarketPhase.AFTER_CLOSE
            and self._after_close_completed_date != trade_date
            and retry_due
            and not planner.has_active_afternoon_freeze(trade_date)
            and all(item.task is not PipelineTask.CLOSE_QUOTES for item in tasks)
        ):
            tasks.append(ScheduledPipelineTask(PipelineTask.CLOSE_QUOTES, now, phase))
        for task in tasks:
            self._state.increment(f"cadence_{task.task.value}_planned")
            if not _scheduled_task_enabled(self._decision_execution_mode, task.task):
                self._state.increment(f"cadence_{task.task.value}_skipped_input_driven")
                continue
            if not self._candidate_codes and task.task not in {
                PipelineTask.FULL_MARKET,
                PipelineTask.REFERENCE_DATA,
                PipelineTask.FREEZE,
                PipelineTask.DEEPSEEK_CUTOFF,
                PipelineTask.CLOSE_QUOTES,
                PipelineTask.CURRENT_QUOTES,
                PipelineTask.LONG_QUOTES,
            }:
                self._state.increment(f"cadence_{task.task.value}_skipped_cold")
                continue
            self._submit_scheduled_task(task)
        if phase is MarketPhase.AFTER_CLOSE and self._after_close_completed_date != trade_date:
            with self._cadence_lock:
                recovery_inflight = PipelineTask.CLOSE_QUOTES in self._scheduled_inflight
            return _after_close_retry_delay(
                batch.next_delay_seconds,
                now,
                retry_at=self._after_close_retry_at,
                inflight=recovery_inflight,
            )
        return batch.next_delay_seconds

    def _submit_scheduled_task(self, scheduled: ScheduledPipelineTask) -> bool:
        task = scheduled.task
        track_inflight = task not in {PipelineTask.TOPK_QUOTES, PipelineTask.LONG_QUOTES}
        if track_inflight:
            with self._cadence_lock:
                if task in self._scheduled_inflight:
                    self._state.increment("cadence_skipped_inflight")
                    self._state.increment(f"cadence_{task.value}_skipped_inflight")
                    return False
                self._scheduled_inflight.add(task)
        is_freeze = task is PipelineTask.FREEZE
        priority = _scheduled_task_priority(task)
        local = shanghai_now(scheduled.scheduled_at)
        event = new_event(
            EventSpec(
                event_type="freeze" if is_freeze else task.value,
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
                    "schedule_point": scheduled.schedule_point.value if scheduled.schedule_point is not None else "",
                    "session_generation": self._current_session_status().generation,
                    "session_trade_date": self._current_session_status().trade_date,
                },
            )
        )
        if task is PipelineTask.TOPK_QUOTES:
            accepted = self._submit_overlay_event(event)
        elif task is PipelineTask.LONG_QUOTES:
            accepted = self._submit_long_quote_event(event)
        else:
            accepted = self.submit_event(event)
        planner = self._cadence
        if planner is not None:
            planner.record_submission(scheduled, accepted=accepted, at=scheduled.scheduled_at)
        if not accepted:
            if track_inflight:
                with self._cadence_lock:
                    self._scheduled_inflight.discard(task)
        else:
            self._state.increment(f"cadence_{task.value}_submitted")
        return accepted

    def _refresh_trading_session(self, at: datetime) -> TradingSessionStatus:
        tracker = getattr(self, "_trading_session", None)
        if not isinstance(tracker, TradingSessionTracker):
            tracker = self._new_trading_session(at)
        return tracker.refresh(at, self._calendar.is_trading_day)

    def _current_session_status(self) -> TradingSessionStatus:
        tracker = getattr(self, "_trading_session", None)
        if not isinstance(tracker, TradingSessionTracker):
            tracker = self._new_trading_session(self._now())
        return tracker.status()

    def _new_trading_session(self, at: datetime) -> TradingSessionTracker:
        tracker = TradingSessionTracker(at)
        tracker.add_rotation_hook(self._handle_session_rotation)
        self._trading_session = tracker
        return tracker

    def _handle_session_rotation(self, status: TradingSessionStatus) -> None:
        if self._cadence is not None:
            self._cadence.rotate_session(
                status.evaluated_at,
                reason=status.discontinuity_reason or "session_rotated",
            )
        self._candidate_codes = ()
        self._candidate_features = ()
        self._market_features = ()
        self._filter_reasons = {}
        self._filter_details = ()
        self._filtered_count = 0
        self._live_overlays = {key: overlay for key, overlay in self._live_overlays.items() if overlay.closing}
        with self._pending_hybrid_lock:
            self._pending_hybrids.clear()
        with self._async_review_lock:
            self._async_review_pending.clear()
        with self._cadence_lock:
            self._scheduled_inflight.intersection_update({PipelineTask.FREEZE})
        self._session_snapshot_ids.clear()
        with self._company_research_membership_lock:
            self._company_research_membership_date = ""
            self._company_research_membership.clear()
            self._company_research_review_barrier = False
            self._company_research_initial_rescore_pending = False

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
                if not self._event_audit.reserve_event(event.audit_record(status=EventStatus.PENDING)):
                    self._state.increment("event_reservation_conflicts")
                    return False
            except Exception as exc:
                self._state.record_error(f"cannot reserve priority event: {str(exc)[:500]}")
                return False
        self._latency.plan(event.event_id, event.event_type)
        accepted, superseded_ids = self._queue.put_with_superseded(event)
        for event_id in superseded_ids:
            self._latency.finish(event_id, outcome="superseded")
            self._event_audit.compare_and_set_event(
                event_id,
                expected_status=EventStatus.PENDING,
                status=EventStatus.FAILED,
                retry_count=0,
                error="superseded by a newer pending input version",
            )
        if accepted:
            self._state.increment("events_submitted")
        else:
            self._latency.finish(event.event_id, outcome="dropped")
            if is_priority:
                self._event_audit.compare_and_set_event(
                    event.event_id,
                    expected_status=EventStatus.PENDING,
                    status=EventStatus.FAILED,
                    retry_count=event.retry_count,
                    error="priority queue full",
                )
                self._state.record_error("priority queue full")
        return accepted


def _scheduled_task_priority(task: PipelineTask) -> EventPriority:
    if task in {
        PipelineTask.FULL_MARKET,
        PipelineTask.CANDIDATE_QUOTES,
        PipelineTask.SCORE,
        PipelineTask.FINAL_CANDIDATE_QUOTES,
    }:
        return EventPriority.MARKET_QUOTES
    if task in {
        PipelineTask.TOPK_QUOTES,
        PipelineTask.LONG_QUOTES,
        PipelineTask.CLOSE_QUOTES,
        PipelineTask.CURRENT_QUOTES,
    }:
        return EventPriority.LIVE_QUOTES
    return {
        PipelineTask.FREEZE: EventPriority.FREEZE,
        PipelineTask.STOCK_RISK: EventPriority.RISK,
        PipelineTask.DEEPSEEK_CUTOFF: EventPriority.DEEPSEEK,
        PipelineTask.INDUSTRY_HEAT: EventPriority.LONG,
        PipelineTask.MARKET_NEWS: EventPriority.LONG,
        PipelineTask.REFERENCE_DATA: EventPriority.LONG,
    }[task]


def _scheduled_task_enabled(decision_execution_mode: str, task: PipelineTask) -> bool:
    return decision_execution_mode != "versioned_dag" or task is not PipelineTask.SCORE


def _scheduled_task_deadline(scheduled: ScheduledPipelineTask) -> datetime | None:
    seconds = task_execution_budget_seconds(scheduled.task)
    if scheduled.task is PipelineTask.CANDIDATE_QUOTES:
        seconds = 23.0
    elif scheduled.task is PipelineTask.SCORE:
        seconds = 38.0
    return scheduled.scheduled_at + timedelta(seconds=seconds) if seconds is not None else None


def _after_close_retry_delay(
    default_delay: float,
    now: datetime,
    *,
    retry_at: datetime | None,
    inflight: bool,
) -> float:
    if inflight or retry_at is None:
        return min(default_delay, 1.0)
    return min(default_delay, max(0.05, (retry_at - now).total_seconds()))
