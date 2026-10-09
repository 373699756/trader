"""Stoppable daily qfq maintenance, independent of market/HTTP deadlines."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import date, datetime

from trader.download.domain.qfq_window import QfqUpdateResult, completed_daily_cutoff
from trader.infra.shutdown import ShutdownDeadline, ShutdownStep

_LOGGER = logging.getLogger(__name__)


class QfqDailyMaintenance:
    def __init__(
        self,
        update: Callable[[Callable[[], bool]], QfqUpdateResult],
        *,
        now: Callable[[], datetime],
        monotonic: Callable[[], float] = time.monotonic,
        needs_initialization: Callable[[], bool] = lambda: False,
    ) -> None:
        self._update = update
        self._now = now
        self._monotonic = monotonic
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._completed_day: date | None = None
        self._next_retry = 0.0
        self._needs_initialization = needs_initialization

    def start(self) -> bool:
        with self._lock:
            if self._thread is not None:
                return False
            self._thread = threading.Thread(target=self._run, name="qfq-daily-maintenance", daemon=False)
            self._thread.start()
            return True

    def stop(self, *, wait: bool, deadline: ShutdownDeadline | None = None) -> ShutdownStep:
        self._cancel.set()
        with self._lock:
            thread = self._thread
        if wait and thread is not None:
            thread.join(None if deadline is None else deadline.remaining_seconds())
        alive = thread is not None and thread.is_alive()
        return ShutdownStep(
            "qfq_daily_maintenance",
            not alive,
            bool(alive and deadline is not None and deadline.expired),
            detail="qfq update remains active" if alive else "",
        )

    def tick(self) -> bool:
        if self._cancel.is_set():
            return False
        now = self._now()
        day = now.date()
        if (
            (completed_daily_cutoff(now) != day and not self._needs_initialization())
            or self._completed_day == day
            or self._monotonic() < self._next_retry
        ):
            return False
        try:
            result = self._update(self._cancel.is_set)
            if not result.pending_codes and result.failure_reason is None and not self._cancel.is_set():
                if completed_daily_cutoff(now) == day:
                    self._completed_day = day
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
            _LOGGER.warning("qfq maintenance failed: %s", type(exc).__name__)
        self._next_retry = self._monotonic() + 1800.0
        return True

    def _run(self) -> None:
        while not self._cancel.is_set():
            try:
                self.tick()
            except (RuntimeError, OSError, ValueError) as exc:
                _LOGGER.warning("qfq maintenance observation failed: %s", type(exc).__name__)
            self._cancel.wait(30.0)
