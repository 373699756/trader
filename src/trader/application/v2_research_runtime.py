"""Lifecycle-owned, non-blocking research intents for the V2 scheduler."""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta

from trader.application.ports.market import ResearchReaderPort, ResearchRefreshResult
from trader.application.ports.v2_runtime import V2CycleRequest, V2ResearchIntent, V2ResearchRuntimeStatus
from trader.application.research_coordination import ResearchCoordinator
from trader.application.runtime.cadence import CadencePolicy, PipelineTask, cadence_band
from trader.application.runtime.schedule import MarketPhase
from trader.application.runtime.shutdown import ShutdownDeadline, ShutdownStep
from trader.domain.recommendation.models import Strategy


class V2ResearchRuntime:
    """Translate published local decisions into bounded company-research work."""

    def __init__(
        self,
        research: ResearchReaderPort,
        *,
        cadence: CadencePolicy,
        now: Callable[[], datetime],
        on_result: Callable[[ResearchRefreshResult, bool], None],
    ) -> None:
        self._cadence = cadence
        self._on_result = on_result
        self._coordinator = ResearchCoordinator(research, now=now, on_result=self._handle_result)
        self._lock = threading.RLock()
        self._trade_date: date | None = None
        self._intents: dict[Strategy, V2ResearchIntent] = {}
        self._membership: set[str] = set()
        self._next_periodic_at: datetime | None = None
        self._pending_initial_codes: dict[Strategy, set[str]] = {}
        self._intent_offer_count = 0
        self._periodic_offer_count = 0
        self._result_count = 0
        self._rescore_result_count = 0

    def start(self) -> bool:
        return self._coordinator.start()

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        return self._coordinator.stop(wait=wait, deadline=deadline)

    def observe(self, intent: V2ResearchIntent, request: V2CycleRequest) -> bool:
        if intent.strategy is not request.strategy or intent.trade_date != request.trade_date:
            raise ValueError("research intent must match its published decision cycle")
        with self._lock:
            self._rotate_locked(intent.trade_date)
            self._intents[intent.strategy] = intent
            current = {code for current_intent in self._intents.values() for code in current_intent.priority_codes}
            newly_entered = tuple(code for code in intent.priority_codes if code not in self._membership)
            self._membership = current
        if not newly_entered:
            with self._lock:
                return bool(self._pending_initial_codes.get(intent.strategy))
        with self._lock:
            pending = self._pending_initial_codes.setdefault(intent.strategy, set())
            pending.update(newly_entered)
        accepted = self._coordinator.offer(newly_entered, request.observed_at)
        active_codes = self._coordinator.active_codes(newly_entered)
        with self._lock:
            if accepted:
                self._intent_offer_count += 1
            current_pending = self._pending_initial_codes.get(intent.strategy)
            if current_pending is not None:
                current_pending.intersection_update(active_codes)
                if not current_pending:
                    self._pending_initial_codes.pop(intent.strategy, None)
            return accepted or bool(active_codes) or bool(self._pending_initial_codes.get(intent.strategy))

    def offer_due(self, at: datetime, phase: MarketPhase, *, is_trading_day: bool) -> bool:
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("research cadence time must be timezone-aware")
        interval = self._cadence.interval(PipelineTask.STOCK_RISK, cadence_band(phase)) if is_trading_day else None
        if interval is None:
            return False
        with self._lock:
            self._rotate_locked(at.date())
            if self._next_periodic_at is not None and at < self._next_periodic_at:
                return False
            codes = tuple(
                dict.fromkeys(
                    code
                    for intent in self._intents.values()
                    for code in (*intent.priority_codes, *intent.candidate_codes)
                )
            )
            if not codes:
                return False
            self._next_periodic_at = at + timedelta(seconds=interval)
        accepted = self._coordinator.offer(codes, at)
        if accepted:
            with self._lock:
                self._periodic_offer_count += 1
        return accepted

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        return self._coordinator.wait_until_idle(timeout_seconds)

    def status(self) -> V2ResearchRuntimeStatus:
        with self._lock:
            trade_date = self._trade_date.isoformat() if self._trade_date is not None else None
            next_periodic_at = self._next_periodic_at.isoformat() if self._next_periodic_at else None
            tracked_strategies = len(self._intents)
            tracked_output_codes = len(self._membership)
            intent_offer_count = self._intent_offer_count
            periodic_offer_count = self._periodic_offer_count
            result_count = self._result_count
            rescore_result_count = self._rescore_result_count
        coordinator = self._coordinator.status()
        return V2ResearchRuntimeStatus(
            state=coordinator.state,
            running_codes=coordinator.running_codes,
            pending_codes=coordinator.pending_codes,
            completed_batches=coordinator.completed_batches,
            partial_batches=coordinator.partial_batches,
            failed_batches=coordinator.failed_batches,
            deferred_codes=coordinator.deferred_codes,
            cooldown_codes=coordinator.cooldown_codes,
            retry_wait_codes=coordinator.retry_wait_codes,
            next_retry_seconds=coordinator.next_retry_seconds,
            gated_offer_codes=coordinator.gated_offer_codes,
            short_circuited_batches=coordinator.short_circuited_batches,
            short_circuited_codes=coordinator.short_circuited_codes,
            tracked_code_gates=coordinator.tracked_code_gates,
            evicted_code_gates=coordinator.evicted_code_gates,
            last_error=coordinator.last_error,
            batch_size=coordinator.batch_size,
            batch_budget_seconds=coordinator.batch_budget_seconds,
            success_cooldown_seconds=coordinator.success_cooldown_seconds,
            retry_delays_seconds=coordinator.retry_delays_seconds,
            trade_date=trade_date,
            tracked_strategies=tracked_strategies,
            tracked_output_codes=tracked_output_codes,
            next_periodic_at=next_periodic_at,
            intent_offer_count=intent_offer_count,
            periodic_offer_count=periodic_offer_count,
            result_count=result_count,
            rescore_result_count=rescore_result_count,
        )

    def _handle_result(self, result: ResearchRefreshResult) -> None:
        with self._lock:
            initial_rescore = self._release_initial_barriers_locked(result)
            self._result_count += 1
            should_rescore = bool(result.changed_codes) or initial_rescore
            if should_rescore:
                self._rescore_result_count += 1
        if should_rescore:
            self._on_result(result, initial_rescore)

    def _rotate_locked(self, trade_date: date) -> None:
        if self._trade_date == trade_date:
            return
        self._trade_date = trade_date
        self._intents.clear()
        self._membership.clear()
        self._next_periodic_at = None
        self._pending_initial_codes.clear()

    def _release_initial_barriers_locked(self, result: ResearchRefreshResult) -> bool:
        if not self._pending_initial_codes:
            return False
        if result.requested_codes and not result.completed_codes:
            self._pending_initial_codes.clear()
            return True
        requested = set(result.requested_codes)
        released = False
        for strategy, pending in tuple(self._pending_initial_codes.items()):
            before = len(pending)
            pending.difference_update(requested)
            if before > 0 and not pending:
                self._pending_initial_codes.pop(strategy, None)
                released = True
        return released


__all__ = ["V2ResearchRuntime"]
