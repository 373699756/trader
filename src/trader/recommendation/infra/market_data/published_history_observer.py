"""Stoppable read-only observation of download-owned published snapshots."""

from __future__ import annotations

import sqlite3
import threading

from trader.recommendation.application.runtime.shutdown import ShutdownDeadline, ShutdownStep
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache


class PublishedHistoryObserver:
    """Rebuild the projection off market deadlines without acquiring a supplier."""

    def __init__(self, history: PublishedHistoryCache, *, interval_seconds: float = 30.0) -> None:
        if interval_seconds <= 0:
            raise ValueError("history observation interval must be positive")
        self._history = history
        self._interval = interval_seconds
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._started = True
            self._history.record_maintenance("loading", stage="reading_active_snapshot")
            self._thread = threading.Thread(target=self._run, name="history-projection-observer", daemon=False)
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
            "history_projection_observer",
            not alive,
            bool(alive and deadline is not None and deadline.expired),
            detail="history projection read remains active" if alive else "",
        )

    def _run(self) -> None:
        while not self._cancel.is_set():
            try:
                self._history.refresh()
                self._history.record_projection_observation()
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._history.record_maintenance("failed", type(exc).__name__, stage="reading_active_snapshot")
            if self._cancel.wait(self._interval):
                return


__all__ = ["PublishedHistoryObserver"]
