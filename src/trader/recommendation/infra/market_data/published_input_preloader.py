"""Cancellable, one-time preload of the process's published input snapshots."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache


class PublishedInputPreloader:
    """Load fixed startup inputs in one owned thread, then finish without polling."""

    def __init__(
        self,
        history: PublishedHistoryCache,
        *,
        initialize: Callable[[Callable[[], bool]], None],
    ) -> None:
        self._profiles: list[tuple[PublishedHistoryCache, Callable[[Callable[[], bool]], None]]] = [
            (history, initialize)
        ]
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False

    def register(self, history: PublishedHistoryCache, initialize: Callable[[Callable[[], bool]], None]) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("published input preload registration is closed")
            self._profiles.append((history, initialize))

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._started = True
            for history, _initialize in self._profiles:
                history.record_maintenance("loading", stage="reading_published_snapshot")
            self._thread = threading.Thread(target=self._run, name="published-input-preloader", daemon=False)
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
            "published_input_preloader",
            not alive,
            bool(alive and deadline is not None and deadline.expired),
            detail="published input preload remains active" if alive else "",
        )

    def _run(self) -> None:
        if self._cancel.is_set():
            return
        for history, initialize in self._profiles:
            if self._cancel.is_set():
                return
            try:
                initialize(self._cancel.is_set)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                history.record_maintenance("failed", type(exc).__name__, stage="startup_preload")


__all__ = ["PublishedInputPreloader"]
