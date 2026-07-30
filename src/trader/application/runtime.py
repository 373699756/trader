"""Explicit scheduler and pipeline lifecycle owner."""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.schedule import MarketPhase, phase_at, seconds_until_next_schedule_boundary
from trader.application.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep

_LOGGER = logging.getLogger(__name__)


class ScheduledPipeline(Protocol):
    def start(self) -> bool: ...

    def stop(
        self,
        timeout_seconds: float = 15.0,
        *,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownReport: ...

    def submit_tick(self, at: datetime | None = None) -> bool: ...

    def submit_due(self, at: datetime | None = None) -> float: ...


@dataclass(frozen=True)
class RuntimeSupervisorConfig:
    now: Callable[[], datetime]
    initializers: Sequence[Callable[[], object]]
    interval_seconds: Callable[[datetime], float]
    shutdown_timeout_seconds: float
    monotonic: Callable[[], float] = time.monotonic
    record_error: Callable[[str], None] | None = None


class RuntimeSupervisor:
    def __init__(
        self,
        pipeline: ScheduledPipeline,
        config: RuntimeSupervisorConfig,
    ) -> None:
        self._pipeline = pipeline
        self._now = config.now
        self._initializers = tuple(config.initializers)
        self._interval_seconds = config.interval_seconds
        self._shutdown_timeout_seconds = max(0.1, config.shutdown_timeout_seconds)
        self._monotonic = config.monotonic
        self._record_error = config.record_error or (lambda _error: None)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._initialized = False
        self._pipeline_started = False
        self._stopped = False
        self._shutdown_report: ShutdownReport | None = None

    def start(self) -> bool:
        with self._lock:
            if self._scheduler is not None and self._scheduler.is_alive():
                return False
            if self._stopped:
                raise RuntimeError("runtime supervisor cannot restart after stop")
            if not self._initialized:
                for initialize in self._initializers:
                    initialize()
                self._initialized = True
            if not self._pipeline.start():
                return False
            self._pipeline_started = True
            self._stop_event.clear()
            scheduler = threading.Thread(target=self._scheduler_loop, name="trader-scheduler", daemon=False)
            self._scheduler = scheduler
            try:
                scheduler.start()
            except BaseException:
                self._stop_pipeline(ShutdownDeadline.start(self._shutdown_timeout_seconds))
                self._pipeline_started = False
                self._scheduler = None
                raise
            return True

    def stop(self, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        deadline = deadline or ShutdownDeadline.start(self._shutdown_timeout_seconds)
        with self._lock:
            if self._stopped:
                return self._shutdown_report or ShutdownReport.from_steps(deadline, ())
            self._stopped = True
            self._stop_event.set()
            scheduler = self._scheduler
        if scheduler is not None and scheduler is not threading.current_thread():
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
        pipeline_step = ShutdownStep(name="pipeline", completed=True, timed_out=False)
        if self._pipeline_started:
            pipeline_step = self._stop_pipeline(deadline)
            self._pipeline_started = False
        if scheduler_timed_out and scheduler is not None:
            self._record_error("scheduler shutdown exceeded timeout")
        report = ShutdownReport.from_steps(deadline, (scheduler_step, pipeline_step))
        with self._lock:
            self._shutdown_report = report
        return report

    def _stop_pipeline(self, deadline: ShutdownDeadline) -> ShutdownStep:
        completed = threading.Event()
        error: list[BaseException] = []
        result: list[object] = []

        def stop_pipeline() -> None:
            try:
                stop_method = self._pipeline.stop
                if "deadline" in inspect.signature(stop_method).parameters:
                    result.append(stop_method(deadline=deadline))
                else:
                    result.append(stop_method(deadline.remaining_seconds()))
            except BaseException as exc:
                error.append(exc)
            finally:
                completed.set()

        stopper = threading.Thread(
            target=stop_pipeline,
            name="trader-pipeline-stop",
            daemon=True,
        )
        stopper.start()
        finished = completed.wait(deadline.remaining_seconds())
        detail = ""
        if error:
            detail = f"pipeline shutdown failed:{type(error[0]).__name__}"
            self._record_error(detail)
        elif not finished:
            detail = "pipeline shutdown exceeded timeout"
            self._record_error(detail)
        pipeline_completed = finished and not error
        pipeline_timed_out = not finished
        if finished and not error and result and isinstance(result[0], ShutdownReport):
            pipeline_completed = result[0].completed
            pipeline_timed_out = result[0].forced or any(step.timed_out for step in result[0].steps)
            if not pipeline_completed:
                detail = "pipeline reported incomplete shutdown"
        return ShutdownStep(
            name="pipeline",
            completed=pipeline_completed,
            timed_out=pipeline_timed_out,
            detail=detail,
        )

    def _scheduler_loop(self) -> None:
        planned_interval = max(0.05, self._interval_seconds(self._now()))
        while not self._stop_event.is_set():
            now = self._now()
            try:
                observe_clock = getattr(self._pipeline, "observe_clock", None)
                if callable(observe_clock):
                    observe_clock(
                        now,
                        monotonic_seconds=self._monotonic(),
                        planned_interval_seconds=planned_interval,
                    )
                submit_due = getattr(self._pipeline, "submit_due", None)
                if callable(submit_due):
                    interval = float(submit_due(now))
                else:
                    self._pipeline.submit_tick(now)
                    interval = self._interval_seconds(self._now())
            except Exception as exc:
                _LOGGER.exception("runtime schedule tick failed")
                self._record_error(str(exc))
                interval = self._interval_seconds(self._now())
            planned_interval = max(0.05, interval)
            self._stop_event.wait(planned_interval)


def scheduler_interval_seconds(at: datetime) -> float:
    phase = phase_at(at, is_trading_day=True)
    maximum = {
        MarketPhase.CLOSED: 30.0,
        MarketPhase.WARMUP: 60.0,
        MarketPhase.TODAY_OBSERVE: 30.0,
        MarketPhase.TODAY_MAIN: 10.0,
        MarketPhase.TODAY_LATE: 20.0,
        MarketPhase.MIDDAY: 60.0,
        MarketPhase.AFTERNOON: 30.0,
        MarketPhase.FINAL_REVIEW: 10.0,
        MarketPhase.DEEPSEEK_CUTOFF: 2.0,
        MarketPhase.FINAL_QUOTE: 2.0,
        MarketPhase.FROZEN: 10.0,
        MarketPhase.AFTER_CLOSE: 60.0,
    }[phase]
    return seconds_until_next_schedule_boundary(at, maximum_seconds=maximum)


__all__ = ["RuntimeSupervisor", "RuntimeSupervisorConfig", "scheduler_interval_seconds"]
