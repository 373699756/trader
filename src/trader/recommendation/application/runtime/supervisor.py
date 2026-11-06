"""Start-and-stop supervisor for the scheduler and its owned background resources."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.infra.shutdown import ObserveShutdownStep, ShutdownDeadline, ShutdownReport, ShutdownStep
from trader.recommendation.application.runtime.schedule import (
    SecondsUntilNextScheduleBoundary,
)

_LOGGER = logging.getLogger(__name__)


class ScheduledRuntime(Protocol):
    def start(self) -> bool: ...

    def stop(self, *, deadline: ShutdownDeadline) -> ShutdownReport: ...

    def submit_due(self, at: datetime | None = None) -> float: ...


@dataclass(frozen=True)
class RuntimeSupervisorConfig:
    now: Callable[[], datetime]
    initializers: Sequence[Callable[[], object]]
    interval_seconds: Callable[[datetime], float]
    shutdown_timeout_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    record_error: Callable[[str], None] | None = None
    deferred_initializers: Sequence[Callable[[], object]] = ()


class RuntimeSupervisor:
    def __init__(
        self,
        runtime: ScheduledRuntime,
        config: RuntimeSupervisorConfig,
    ) -> None:
        self._runtime = runtime
        self._now = config.now
        self._initializers = tuple(config.initializers)
        self._interval_seconds = config.interval_seconds
        self._shutdown_timeout_seconds = max(0.1, config.shutdown_timeout_seconds)
        self._monotonic = config.monotonic
        self._record_error = config.record_error or (lambda _error: None)
        self._deferred_initializers = tuple(config.deferred_initializers)
        self._lock = threading.Lock()
        self._initialization_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._deferred_stop_event = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._deferred_thread: threading.Thread | None = None
        self._initialized = False
        self._starting = False
        self._runtime_started = False
        self._stopped = False
        self._shutdown_report: ShutdownReport | None = None

    def initialize(self) -> None:
        """Prepare synchronous dependencies without starting workers or scheduling."""
        with self._initialization_lock:
            with self._lock:
                if self._stopped:
                    raise RuntimeError("runtime supervisor cannot initialize after stop")
                if self._initialized:
                    return
            for initialize in self._initializers:
                initializer_started = self._monotonic()
                try:
                    initialize()
                finally:
                    _LOGGER.info(
                        "startup initializer completed name=%s elapsed_ms=%.1f",
                        getattr(initialize, "__qualname__", repr(initialize)),
                        max(0.0, self._monotonic() - initializer_started) * 1000.0,
                    )
            with self._lock:
                self._initialized = True

    def start(self) -> bool:
        with self._lock:
            if self._scheduler is not None and self._scheduler.is_alive():
                return False
            if self._starting:
                return False
            if self._stopped:
                raise RuntimeError("runtime supervisor cannot restart after stop")
            self._starting = True
        startup_started = self._monotonic()
        try:
            self.initialize()
            with self._lock:
                if self._stopped:
                    return False
            if not self._runtime.start():
                return False
            with self._lock:
                self._runtime_started = True
                self._stop_event.clear()
                scheduler = threading.Thread(target=self._scheduler_loop, name="trader-scheduler", daemon=False)
                self._scheduler = scheduler
                scheduler.start()
                self._start_deferred_initializers_locked()
            _LOGGER.info(
                "runtime startup completed elapsed_ms=%.1f deferred_initializers=%d",
                max(0.0, self._monotonic() - startup_started) * 1000.0,
                len(self._deferred_initializers),
            )
            return True
        except BaseException:
            self.stop(ShutdownDeadline.start(self._shutdown_timeout_seconds, monotonic=self._monotonic))
            raise
        finally:
            with self._lock:
                self._starting = False

    def stop(self, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        deadline = deadline or ShutdownDeadline.start(self._shutdown_timeout_seconds, monotonic=self._monotonic)
        with ObserveShutdownStep(deadline, "supervisor:state"), self._lock:
            if self._stopped:
                return self._shutdown_report or ShutdownReport.from_steps(deadline, ())
            self._stopped = True
            self._stop_event.set()
            self._deferred_stop_event.set()
            scheduler = self._scheduler
            deferred = self._deferred_thread
        if deferred is not None and deferred is not threading.current_thread() and deferred.ident is not None:
            with ObserveShutdownStep(deadline, "supervisor:deferred_initializers"):
                deferred.join(deadline.remaining_seconds())
            deferred_timed_out = deferred.is_alive()
        else:
            deferred_timed_out = False
        deferred_step = ShutdownStep(
            name="deferred_initializers",
            completed=not deferred_timed_out,
            timed_out=deferred_timed_out,
            detail="deferred initializer shutdown exceeded timeout" if deferred_timed_out else "",
        )
        if scheduler is not None and scheduler is not threading.current_thread() and scheduler.ident is not None:
            with ObserveShutdownStep(deadline, "supervisor:scheduler"):
                scheduler.join(deadline.remaining_seconds())
            scheduler_timed_out = scheduler.is_alive()
        else:
            scheduler_timed_out = False
        scheduler_step = ShutdownStep(
            name="scheduler",
            completed=not scheduler_timed_out,
            timed_out=scheduler_timed_out,
            detail="scheduler shutdown exceeded timeout" if scheduler_timed_out else "",
        )
        runtime_step = ShutdownStep(name="runtime", completed=True, timed_out=False)
        if self._runtime_started:
            with ObserveShutdownStep(deadline, "supervisor:runtime"):
                runtime_step = self._stop_runtime(deadline)
            self._runtime_started = False
        if scheduler_timed_out and scheduler is not None:
            self._record_error("scheduler shutdown exceeded timeout")
        if deferred_timed_out:
            self._record_error("deferred initializer shutdown exceeded timeout")
        report = ShutdownReport.from_steps(deadline, (scheduler_step, runtime_step, deferred_step))
        with self._lock:
            self._shutdown_report = report
        return report

    def _start_deferred_initializers_locked(self) -> None:
        if not self._deferred_initializers or (self._deferred_thread is not None and self._deferred_thread.is_alive()):
            return
        self._deferred_stop_event.clear()
        deferred = threading.Thread(
            target=self._run_deferred_initializers,
            name="trader-deferred-initializer",
            daemon=True,
        )
        self._deferred_thread = deferred
        deferred.start()

    def _run_deferred_initializers(self) -> None:
        for initialize in self._deferred_initializers:
            if self._deferred_stop_event.is_set():
                return
            initializer_started = self._monotonic()
            try:
                initialize()
            except Exception as exc:
                name = getattr(initialize, "__qualname__", repr(initialize))
                self._record_error(f"deferred initializer failed:{name}:{type(exc).__name__}")
                _LOGGER.exception("deferred startup initializer failed name=%s", name)
            finally:
                _LOGGER.info(
                    "deferred startup initializer completed name=%s elapsed_ms=%.1f",
                    getattr(initialize, "__qualname__", repr(initialize)),
                    max(0.0, self._monotonic() - initializer_started) * 1000.0,
                )

    def _stop_runtime(self, deadline: ShutdownDeadline) -> ShutdownStep:
        completed = threading.Event()
        error: list[BaseException] = []
        result: list[object] = []

        def stop_runtime() -> None:
            try:
                result.append(self._runtime.stop(deadline=deadline))
            except BaseException as exc:
                error.append(exc)
            finally:
                completed.set()

        stopper = threading.Thread(
            target=stop_runtime,
            name="trader-runtime-stop",
            daemon=True,
        )
        stopper.start()
        finished = completed.wait(deadline.remaining_seconds())
        detail = ""
        if error:
            detail = f"runtime shutdown failed:{type(error[0]).__name__}"
            self._record_error(detail)
        elif not finished:
            detail = "runtime shutdown exceeded timeout"
            self._record_error(detail)
        pipeline_completed = finished and not error
        pipeline_timed_out = not finished
        if finished and not error and result and isinstance(result[0], ShutdownReport):
            pipeline_completed = result[0].completed
            pipeline_timed_out = result[0].forced or any(step.timed_out for step in result[0].steps)
            if not pipeline_completed:
                detail = "runtime reported incomplete shutdown"
        return ShutdownStep(
            name="runtime",
            completed=pipeline_completed,
            timed_out=pipeline_timed_out,
            detail=detail,
        )

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            now = self._now()
            try:
                interval = float(self._runtime.submit_due(now))
            except Exception as exc:
                _LOGGER.exception("runtime schedule tick failed")
                self._record_error(str(exc))
                interval = self._interval_seconds(self._now())
            self._stop_event.wait(max(0.05, interval))


def SchedulerIntervalSeconds(at: datetime) -> float:
    return SecondsUntilNextScheduleBoundary(at, maximum_seconds=10.0)


__all__ = ["RuntimeSupervisor", "RuntimeSupervisorConfig", "SchedulerIntervalSeconds"]
