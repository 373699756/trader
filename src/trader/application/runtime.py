"""Explicit scheduler and pipeline lifecycle owner."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from trader.application.schedule import MarketPhase, phase_at, seconds_until_next_schedule_boundary

_LOGGER = logging.getLogger(__name__)


class ScheduledPipeline(Protocol):
    def start(self) -> bool: ...

    def stop(self, timeout_seconds: float = 15.0) -> None: ...

    def submit_tick(self, at: datetime | None = None) -> bool: ...

    def submit_due(self, at: datetime | None = None) -> float: ...


@dataclass(frozen=True)
class RuntimeSupervisorConfig:
    now: Callable[[], datetime]
    initializers: Sequence[Callable[[], object]]
    interval_seconds: Callable[[datetime], float]
    shutdown_timeout_seconds: float
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
        self._record_error = config.record_error or (lambda _error: None)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._initialized = False
        self._pipeline_started = False
        self._stopped = False

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
                self._pipeline.stop(self._shutdown_timeout_seconds)
                self._pipeline_started = False
                self._scheduler = None
                raise
            return True

    def stop(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_event.set()
            scheduler = self._scheduler
        if scheduler is not None and scheduler is not threading.current_thread():
            scheduler.join(self._shutdown_timeout_seconds)
            scheduler_timed_out = scheduler.is_alive()
        else:
            scheduler_timed_out = False
        if self._pipeline_started:
            self._pipeline.stop(self._shutdown_timeout_seconds)
            self._pipeline_started = False
        if scheduler_timed_out and scheduler is not None:
            self._record_error("scheduler shutdown exceeded timeout")
            scheduler.join()

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            now = self._now()
            try:
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
            self._stop_event.wait(max(0.05, interval))


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
