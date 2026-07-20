"""Event submission and cadence scheduling mixin for the recommendation pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, cast

from trader.application.cadence import PipelineTask, ScheduledPipelineTask
from trader.application.events import EventPriority, PipelineEvent, new_event
from trader.application.pipeline_state import PipelineState
from trader.application.pipeline_workers import persist
from trader.application.schedule import (
    MarketPhase,
    SchedulePoint,
    decision_at,
    schedule_point_at,
    shanghai_now,
    trade_date_at,
)

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline


class PipelineSubmissionMixin(PipelineState):
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
                if not persist(
                    cast("RecommendationPipeline", self),
                    self._event_audit.reserve_event,
                    event.audit_record(status="pending"),
                ):
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
