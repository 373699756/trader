"""Explicit ownership for process-local background schedulers."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Protocol, TypedDict

from . import config, deepseek_scheduler

_LOGGER = logging.getLogger(__name__)


class StoppableScheduler(Protocol):
    def start(self) -> bool: ...

    def stop(self, timeout_seconds: float = 5.0) -> None: ...


class RuntimeSupervisorStatus(TypedDict):
    started: bool
    owns_realtime_scheduler: bool
    owns_deepseek_scheduler: bool
    owns_validation_workers: bool
    manages_transient_workers: bool


class RuntimeSupervisor:
    """Starts and stops the background components owned by one app process."""

    def __init__(
        self,
        realtime_scheduler: StoppableScheduler | None,
        *,
        start_deepseek: Callable[[], bool] | None = None,
        stop_deepseek: Callable[[float], None] | None = None,
        start_validation_workers: Callable[[], bool] | None = None,
        stop_validation_workers: Callable[[float], None] | None = None,
        stop_transient_workers: Callable[[float], None] | None = None,
    ) -> None:
        self._realtime_scheduler = realtime_scheduler
        self._start_deepseek = start_deepseek or deepseek_scheduler.start_deepseek_scheduler
        self._stop_deepseek = stop_deepseek or deepseek_scheduler.stop_deepseek_scheduler
        self._start_validation_workers = start_validation_workers
        self._stop_validation_workers = stop_validation_workers
        self._stop_transient_workers = stop_transient_workers
        self._lock = threading.Lock()
        self._started = False
        self._owns_realtime = False
        self._owns_deepseek = False
        self._owns_validation_workers = False

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._started = True
        try:
            if self._realtime_scheduler is not None and bool(
                getattr(config, "REALTIME_MARKET_SCHEDULER_ENABLED", True)
            ):
                self._owns_realtime = self._realtime_scheduler.start()
            if bool(getattr(config, "DEEPSEEK_INTERNAL_SCHEDULER_ENABLED", True)):
                self._owns_deepseek = self._start_deepseek()
            if self._start_validation_workers is not None and bool(
                getattr(config, "WEB_BACKGROUND_WORKERS_ENABLED", False)
            ):
                self._owns_validation_workers = self._start_validation_workers()
            return True
        except Exception:
            self.stop()
            raise

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            owns_realtime = self._owns_realtime
            owns_deepseek = self._owns_deepseek
            self._owns_realtime = False
            self._owns_deepseek = False
            self._owns_validation_workers = False
            self._started = False
        if owns_realtime and self._realtime_scheduler is not None:
            self._stop_safely("realtime scheduler", self._realtime_scheduler.stop, timeout_seconds)
        if self._stop_validation_workers is not None:
            self._stop_safely("validation workers", self._stop_validation_workers, timeout_seconds)
        if owns_deepseek:
            self._stop_safely("DeepSeek scheduler", self._stop_deepseek, timeout_seconds)
        if self._stop_transient_workers is not None:
            self._stop_safely("transient workers", self._stop_transient_workers, timeout_seconds)

    @staticmethod
    def _stop_safely(
        component_name: str,
        stop_component: Callable[[float], None],
        timeout_seconds: float,
    ) -> None:
        try:
            stop_component(timeout_seconds)
        except Exception:
            _LOGGER.exception("failed to stop %s", component_name)

    def status(self) -> RuntimeSupervisorStatus:
        with self._lock:
            return {
                "started": self._started,
                "owns_realtime_scheduler": self._owns_realtime,
                "owns_deepseek_scheduler": self._owns_deepseek,
                "owns_validation_workers": self._owns_validation_workers,
                "manages_transient_workers": self._stop_transient_workers is not None,
            }


__all__ = ["RuntimeSupervisor", "RuntimeSupervisorStatus", "StoppableScheduler"]
