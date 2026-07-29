"""Non-blocking, bounded coordination for candidate company research."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from trader.application.ports.market import ResearchReaderPort, ResearchRefreshResult
from trader.application.workers import BoundedExecutor


@dataclass(frozen=True)
class ResearchCoordinatorOptions:
    batch_size: int = 4
    batch_budget_seconds: float = 40.0
    queue_capacity: int = 1
    monotonic: Callable[[], float] = time.monotonic


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
        self._runner_active = False
        self._started = False
        self._stopping = False
        self._completed_batches = 0
        self._partial_batches = 0
        self._failed_batches = 0
        self._deferred_codes = 0
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

    def stop(self, *, wait: bool, timeout_seconds: float | None = None) -> None:
        deadline = None if timeout_seconds is None else self._monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            self._pending.clear()
            while wait and self._runner_active:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    break
                self._condition.wait(remaining)
            drained = not self._runner_active
        self._executor.stop(wait=wait and drained, cancel_futures=True)
        with self._condition:
            self._started = False
            self._condition.notify_all()

    def offer(self, codes: Sequence[str], observed_at: datetime) -> bool:
        normalized = tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))
        if not normalized:
            return False
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("research observation time must be timezone-aware")
        with self._condition:
            if not self._started or self._stopping:
                return False
            running = set(self._running_codes)
            prioritized = [code for code in normalized if code not in running]
            existing = [code for code in self._pending if code not in running and code not in prioritized]
            self._pending = [*prioritized, *existing]
            if self._latest_observed_at is None or observed_at > self._latest_observed_at:
                self._latest_observed_at = observed_at
            if self._runner_active:
                self._condition.notify_all()
                return True
            self._runner_active = True
            future = self._executor.submit(self._drain)
            if future is None:
                self._runner_active = False
                self._failed_batches += 1
                self._last_error = "research_coordinator_queue_rejected"
                self._condition.notify_all()
                return False
            return True

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        deadline = self._monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while self._runner_active or self._pending:
                remaining = deadline - self._monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return True

    def status(self) -> Mapping[str, object]:
        with self._condition:
            state = "stopped"
            if self._started and not self._stopping:
                state = "running" if self._runner_active else "idle"
            elif self._stopping and self._runner_active:
                state = "stopping"
            return {
                "state": state,
                "running_codes": len(self._running_codes),
                "pending_codes": len(self._pending),
                "completed_batches": self._completed_batches,
                "partial_batches": self._partial_batches,
                "failed_batches": self._failed_batches,
                "deferred_codes": self._deferred_codes,
                "last_error": self._last_error,
                "batch_size": self._batch_size,
                "batch_budget_seconds": self._batch_budget_seconds,
            }

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
                self._record_result(result)
                try:
                    self._on_result(result)
                except Exception as exc:
                    with self._condition:
                        self._last_error = f"research_result_callback_failed:{type(exc).__name__}"
        finally:
            with self._condition:
                self._running_codes = ()
                self._runner_active = False
                self._condition.notify_all()
                restart = bool(self._pending) and self._started and not self._stopping
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

    def _record_result(self, result: ResearchRefreshResult) -> None:
        with self._condition:
            self._completed_batches += 1
            if result.partial_codes or result.deferred_codes:
                self._partial_batches += 1
            if result.failed_codes:
                self._failed_batches += 1
            self._deferred_codes += len(result.deferred_codes)
            self._condition.notify_all()

    def _restart_drain(self) -> None:
        with self._condition:
            if self._runner_active or self._stopping or not self._started or not self._pending:
                return
            self._runner_active = True
            future = self._executor.submit(self._drain)
            if future is None:
                self._runner_active = False
                self._failed_batches += 1
                self._last_error = "research_coordinator_queue_rejected"
                self._condition.notify_all()


__all__ = ["ResearchCoordinator", "ResearchCoordinatorOptions"]
