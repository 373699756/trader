"""Non-blocking, bounded coordination for candidate company research."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trader.application.ports.market import ResearchReaderPort, ResearchRefreshResult
from trader.application.shutdown import ShutdownDeadline, ShutdownStep
from trader.application.workers import BoundedExecutor


@dataclass(frozen=True)
class ResearchCoordinatorOptions:
    batch_size: int = 4
    batch_budget_seconds: float = 40.0
    queue_capacity: int = 1
    success_cooldown_seconds: float = 60.0
    retry_delays_seconds: tuple[float, ...] = (60.0, 120.0, 240.0, 480.0, 900.0)
    state_capacity: int = 2048
    monotonic: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class ResearchCoordinatorStatus:
    state: str
    running_codes: int
    pending_codes: int
    completed_batches: int
    partial_batches: int
    failed_batches: int
    deferred_codes: int
    cooldown_codes: int
    retry_wait_codes: int
    next_retry_seconds: float
    gated_offer_codes: int
    short_circuited_batches: int
    short_circuited_codes: int
    tracked_code_gates: int
    evicted_code_gates: int
    last_error: str
    batch_size: int
    batch_budget_seconds: float
    success_cooldown_seconds: float
    retry_delays_seconds: tuple[float, ...]


@dataclass(frozen=True)
class _CodeGate:
    eligible_at: float
    failure_count: int


class ResearchCoordinator:
    def __init__(
        self,
        research: ResearchReaderPort,
        *,
        now: Callable[[], datetime],
        on_result: Callable[[ResearchRefreshResult], None],
        options: ResearchCoordinatorOptions | None = None,
    ) -> None:
        settings = options or ResearchCoordinatorOptions()
        self._research = research
        self._now = now
        self._on_result = on_result
        self._batch_size = max(1, settings.batch_size)
        self._batch_budget_seconds = max(0.1, settings.batch_budget_seconds)
        self._success_cooldown_seconds = max(0.1, settings.success_cooldown_seconds)
        retry_delays = tuple(max(0.1, item) for item in settings.retry_delays_seconds)
        self._retry_delays_seconds = retry_delays or (60.0,)
        self._state_capacity = max(self._batch_size, settings.state_capacity)
        self._monotonic = settings.monotonic
        self._executor = BoundedExecutor(
            worker_count=1,
            queue_capacity=max(1, settings.queue_capacity),
            thread_name_prefix="company-research",
        )
        self._condition = threading.Condition()
        self._pending: list[str] = []
        self._running_codes: tuple[str, ...] = ()
        self._latest_observed_at: datetime | None = None
        self._code_gates: dict[str, _CodeGate] = {}
        self._global_retry_at: float | None = None
        self._global_failure_count = 0
        self._runner_active = False
        self._started = False
        self._stopping = False
        self._completed_batches = 0
        self._partial_batches = 0
        self._failed_batches = 0
        self._deferred_codes = 0
        self._gated_offer_codes = 0
        self._short_circuited_batches = 0
        self._short_circuited_codes = 0
        self._evicted_code_gates = 0
        self._last_error = ""

    def start(self) -> bool:
        with self._condition:
            if self._started:
                return False
            if self._stopping:
                raise RuntimeError("research coordinator cannot restart after stop")
            self._executor.start()
            self._started = True
            return True

    def stop(
        self,
        *,
        wait: bool,
        timeout_seconds: float | None = None,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        effective_deadline = deadline
        if effective_deadline is None and timeout_seconds is not None:
            effective_deadline = ShutdownDeadline.start(timeout_seconds, monotonic=self._monotonic)
        with self._condition:
            self._stopping = True
            self._pending.clear()
            while wait and self._runner_active:
                if effective_deadline is None:
                    self._condition.wait()
                    continue
                remaining = effective_deadline.remaining_seconds()
                if remaining <= 0.0:
                    break
                self._condition.wait(remaining)
            drained = not self._runner_active
        executor_step = self._executor.stop(
            wait=wait and drained,
            cancel_futures=True,
            deadline=effective_deadline,
        )
        with self._condition:
            self._started = False
            self._condition.notify_all()
        completed = drained and executor_step.completed
        return ShutdownStep(
            name="research",
            completed=completed,
            timed_out=not completed and effective_deadline is not None and effective_deadline.expired,
            cancelled_count=executor_step.cancelled_count,
            detail="research batch remains inflight" if not completed else "",
        )

    def offer(self, codes: Sequence[str], observed_at: datetime) -> bool:
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized:
            return False
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("research observation time must be timezone-aware")
        with self._condition:
            return self._offer_locked(normalized, observed_at)

    def active_codes(self, codes: Sequence[str]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        with self._condition:
            active = set(self._running_codes) | set(self._pending)
            return tuple(code for code in normalized if code in active)

    def _offer_locked(self, normalized: tuple[str, ...], observed_at: datetime) -> bool:
        if not self._started or self._stopping:
            return False
        current_tick = self._monotonic()
        if self._latest_observed_at is None or observed_at > self._latest_observed_at:
            self._latest_observed_at = observed_at
        running = set(self._running_codes)
        if self._global_retry_at is not None and current_tick < self._global_retry_at:
            self._record_global_backoff_offer_locked(normalized, running, self._global_retry_at)
            return False
        self._global_retry_at = None
        prioritized = self._eligible_offer_codes_locked(normalized, running, current_tick)
        if not prioritized:
            return False
        return self._queue_offer_locked(prioritized, running)

    def _record_global_backoff_offer_locked(
        self,
        normalized: tuple[str, ...],
        running: set[str],
        retry_at: float,
    ) -> None:
        blocked_codes = tuple(code for code in normalized if code not in running)
        for code in blocked_codes:
            current = self._code_gates.get(code)
            self._code_gates[code] = _CodeGate(
                eligible_at=max(retry_at, current.eligible_at if current is not None else 0.0),
                failure_count=max(1, current.failure_count if current is not None else 0),
            )
        self._gated_offer_codes += len(blocked_codes)
        self._trim_code_gates_locked()

    def _eligible_offer_codes_locked(
        self,
        normalized: tuple[str, ...],
        running: set[str],
        current_tick: float,
    ) -> list[str]:
        prioritized: list[str] = []
        for code in normalized:
            gate = self._code_gates.get(code)
            if code in running:
                continue
            if gate is not None and current_tick < gate.eligible_at:
                self._gated_offer_codes += 1
                continue
            prioritized.append(code)
        return prioritized

    def _queue_offer_locked(self, prioritized: list[str], running: set[str]) -> bool:
        previous_pending = tuple(self._pending)
        existing = [code for code in self._pending if code not in running and code not in prioritized]
        self._pending = [*prioritized, *existing]
        if self._runner_active:
            self._condition.notify_all()
            return tuple(self._pending) != previous_pending
        self._runner_active = True
        future = self._executor.submit(self._drain)
        if future is not None:
            return True
        self._runner_active = False
        self._failed_batches += 1
        self._last_error = "research_coordinator_queue_rejected"
        self._condition.notify_all()
        return False

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        deadline = self._monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._runner_active or self._pending:
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True

    def status(self) -> ResearchCoordinatorStatus:
        with self._condition:
            current_tick = self._monotonic()
            state = "stopped"
            if self._started and not self._stopping:
                state = "running" if self._runner_active else "idle"
            elif self._stopping and self._runner_active:
                state = "stopping"
            cooldown_codes = sum(
                1 for gate in self._code_gates.values() if gate.failure_count == 0 and current_tick < gate.eligible_at
            )
            retry_gates = tuple(
                gate for gate in self._code_gates.values() if gate.failure_count > 0 and current_tick < gate.eligible_at
            )
            retry_deadlines = [gate.eligible_at for gate in retry_gates]
            if self._global_retry_at is not None and current_tick < self._global_retry_at:
                retry_deadlines.append(self._global_retry_at)
            next_retry_seconds = round(max(0.0, min(retry_deadlines) - current_tick), 3) if retry_deadlines else 0.0
            return ResearchCoordinatorStatus(
                state=state,
                running_codes=len(self._running_codes),
                pending_codes=len(self._pending),
                completed_batches=self._completed_batches,
                partial_batches=self._partial_batches,
                failed_batches=self._failed_batches,
                deferred_codes=self._deferred_codes,
                cooldown_codes=cooldown_codes,
                retry_wait_codes=len(retry_gates),
                next_retry_seconds=next_retry_seconds,
                gated_offer_codes=self._gated_offer_codes,
                short_circuited_batches=self._short_circuited_batches,
                short_circuited_codes=self._short_circuited_codes,
                tracked_code_gates=len(self._code_gates),
                evicted_code_gates=self._evicted_code_gates,
                last_error=self._last_error,
                batch_size=self._batch_size,
                batch_budget_seconds=self._batch_budget_seconds,
                success_cooldown_seconds=self._success_cooldown_seconds,
                retry_delays_seconds=self._retry_delays_seconds,
            )

    def _drain(self) -> None:
        try:
            while True:
                with self._condition:
                    if self._stopping or not self._pending:
                        return
                    batch = tuple(self._pending[: self._batch_size])
                    del self._pending[: self._batch_size]
                    self._running_codes = batch
                    observed_at = self._latest_observed_at or self._now()
                result = self._run_batch(batch, observed_at)
                should_stop = self._record_result(result)
                try:
                    self._on_result(result)
                except Exception as exc:
                    with self._condition:
                        self._last_error = f"research_result_callback_failed:{type(exc).__name__}"
                if should_stop:
                    return
        finally:
            with self._condition:
                self._running_codes = ()
                self._runner_active = False
                self._condition.notify_all()
                current_tick = self._monotonic()
                retry_blocked = self._global_retry_at is not None and current_tick < self._global_retry_at
                restart = bool(self._pending) and self._started and not self._stopping and not retry_blocked
            if restart:
                self._restart_drain()

    def _run_batch(self, batch: tuple[str, ...], observed_at: datetime) -> ResearchRefreshResult:
        started_at = self._now()
        deadline = started_at + timedelta(seconds=self._batch_budget_seconds)
        try:
            return self._research.refresh_stock_risk(batch, observed_at, deadline=deadline)
        except Exception as exc:
            completed_at = self._now()
            with self._condition:
                self._last_error = f"research_batch_failed:{type(exc).__name__}"
            return ResearchRefreshResult(
                requested_codes=batch,
                failed_codes=batch,
                data_version=f"research-failed:{completed_at.isoformat()}",
                started_at=started_at,
                completed_at=completed_at,
                deadline_reached=completed_at >= deadline,
            )

    def _record_result(self, result: ResearchRefreshResult) -> bool:
        with self._condition:
            current_tick = self._monotonic()
            self._completed_batches += 1
            if result.partial_codes or result.deferred_codes:
                self._partial_batches += 1
            if result.failed_codes:
                self._failed_batches += 1
            self._deferred_codes += len(result.deferred_codes)
            requested = set(result.requested_codes)
            completed = set(result.completed_codes)
            partial = set(result.partial_codes)
            terminal_failures = set(result.failed_codes) | set(result.deferred_codes)
            missing = requested - completed - terminal_failures
            retry_codes = partial | terminal_failures | missing
            successful_codes = completed - partial
            for code in successful_codes:
                self._code_gates[code] = _CodeGate(
                    eligible_at=current_tick + self._success_cooldown_seconds,
                    failure_count=0,
                )
            for code in retry_codes:
                self._record_code_failure_locked(code, current_tick)
            full_failure = bool(requested) and not completed and requested <= retry_codes
            if full_failure:
                self._global_failure_count += 1
                retry_at = current_tick + self._retry_delay(self._global_failure_count)
                self._global_retry_at = retry_at
                pending = tuple(self._pending)
                self._pending.clear()
                for code in (*result.requested_codes, *pending):
                    current = self._code_gates.get(code)
                    self._code_gates[code] = _CodeGate(
                        eligible_at=max(retry_at, current.eligible_at if current is not None else 0.0),
                        failure_count=max(1, current.failure_count if current is not None else 0),
                    )
                self._short_circuited_batches += 1
                self._short_circuited_codes += len(pending)
                self._last_error = "research_batch_full_failure_backoff"
            elif completed:
                self._global_failure_count = 0
                self._global_retry_at = None
            self._trim_code_gates_locked()
            self._condition.notify_all()
            return full_failure

    def _restart_drain(self) -> None:
        with self._condition:
            if self._runner_active or self._stopping or not self._started or not self._pending:
                return
            current_tick = self._monotonic()
            if self._global_retry_at is not None and current_tick < self._global_retry_at:
                return
            self._runner_active = True
            future = self._executor.submit(self._drain)
            if future is None:
                self._runner_active = False
                self._failed_batches += 1
                self._last_error = "research_coordinator_queue_rejected"
                self._condition.notify_all()

    def _record_code_failure_locked(self, code: str, current_tick: float) -> None:
        current = self._code_gates.get(code)
        failure_count = (current.failure_count if current is not None else 0) + 1
        self._code_gates[code] = _CodeGate(
            eligible_at=current_tick + self._retry_delay(failure_count),
            failure_count=failure_count,
        )

    def _retry_delay(self, failure_count: int) -> float:
        index = min(max(1, failure_count), len(self._retry_delays_seconds)) - 1
        return self._retry_delays_seconds[index]

    def _trim_code_gates_locked(self) -> None:
        excess = len(self._code_gates) - self._state_capacity
        if excess <= 0:
            return
        protected = set(self._pending) | set(self._running_codes)
        removable = sorted(
            ((code, gate) for code, gate in self._code_gates.items() if code not in protected),
            key=lambda item: item[1].eligible_at,
        )
        for code, _gate in removable[:excess]:
            self._code_gates.pop(code, None)
            self._evicted_code_gates += 1


__all__ = ["ResearchCoordinator", "ResearchCoordinatorOptions", "ResearchCoordinatorStatus"]
