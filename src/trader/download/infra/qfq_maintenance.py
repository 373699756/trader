"""Stoppable daily qfq maintenance, independent of market/HTTP deadlines."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import date, datetime
from datetime import time as daily_time

from trader.download.domain.qfq_window import QfqUpdateResult
from trader.infra.shutdown import ShutdownDeadline, ShutdownStep

_LOGGER = logging.getLogger(__name__)


class QfqDailyMaintenance:
    def __init__(
        self,
        update: Callable[[Callable[[], bool]], QfqUpdateResult],
        *,
        now: Callable[[], datetime],
        enabled: bool = True,
    ) -> None:
        self._update = update
        self._now = now
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started_at: datetime | None = None
        self._attempted_on: date | None = None
        self._enabled = enabled

    def start(self) -> bool:
        if not self._enabled:
            return False
        with self._lock:
            if self._thread is not None:
                return False
            self._started_at = self._now()
            self._thread = threading.Thread(target=self._run, name="qfq-daily-maintenance", daemon=False)
            self._thread.start()
            return True

    def stop(self, *, wait: bool, deadline: ShutdownDeadline | None = None) -> ShutdownStep:
        if not self._enabled:
            return ShutdownStep("qfq_daily_maintenance", True, False, detail="disabled for secondary profile")
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
        if self._started_at is None:
            self._started_at = now
        trigger = now.replace(hour=15, minute=10, second=0, microsecond=0)
        if (
            self._started_at >= trigger
            or not daily_time(15, 10) <= now.time() < daily_time(15, 11)
            or self._attempted_on == now.date()
        ):
            return False
        self._attempted_on = now.date()
        try:
            self._update(self._cancel.is_set)
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
            _LOGGER.warning("qfq maintenance failed: %s", type(exc).__name__)
        return True

    def _run(self) -> None:
        while not self._cancel.is_set():
            try:
                self.tick()
            except (RuntimeError, OSError, ValueError) as exc:
                _LOGGER.warning("qfq maintenance observation failed: %s", type(exc).__name__)
            self._cancel.wait(30.0)
