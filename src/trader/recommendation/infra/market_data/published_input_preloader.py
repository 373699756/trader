"""One owned worker for fixed startup inputs and publication-identity observations."""

from __future__ import annotations

import math
import logging
import sqlite3
import threading
from collections.abc import Callable

from trader.infra.shutdown import ShutdownDeadline, ShutdownStep
from trader.recommendation.infra.market_data.published_history_cache import PublishedHistoryCache

_LOGGER = logging.getLogger(__name__)


class PublishedInputPreloader:
    """Keep startup inputs fixed while observing explicitly registered publications."""

    def __init__(
        self,
        history: PublishedHistoryCache,
        *,
        initialize: Callable[[Callable[[], bool]], None],
        observation_interval_seconds: float = 30.0,
    ) -> None:
        if not math.isfinite(observation_interval_seconds) or observation_interval_seconds <= 0.0:
            raise ValueError("publication observation interval must be positive")
        self._profiles: list[tuple[PublishedHistoryCache, Callable[[Callable[[], bool]], None]]] = [
            (history, initialize)
        ]
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._started = False
        self._observers: list[Callable[[Callable[[], bool]], None]] = []
        self._startup_observers: list[Callable[[], None]] = []
        self._observation_interval = observation_interval_seconds

    def register(self, history: PublishedHistoryCache, initialize: Callable[[Callable[[], bool]], None]) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("published input preload registration is closed")
            self._profiles.append((history, initialize))

    def register_publication_observer(self, observer: Callable[[Callable[[], bool]], None]) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("published input observer registration is closed")
            if observer not in self._observers:
                self._observers.append(observer)

    def register_startup_observer(self, observer: Callable[[], None]) -> None:
        with self._lock:
            if self._started:
                raise RuntimeError("published input startup observer registration is closed")
            if observer not in self._startup_observers:
                self._startup_observers.append(observer)

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
        # Every profile's fixed scoring input precedes slow maintenance. These
        # observations must not close a successfully initialized input gate.
        for observe_startup in self._startup_observers:
            if self._cancel.is_set():
                return
            try:
                observe_startup()
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                _LOGGER.error("启动维护观察失败: category=%s", type(exc).__name__)
        while self._observers and not self._cancel.wait(self._observation_interval):
            for observe in self._observers:
                if self._cancel.is_set():
                    return
                observe(self._cancel.is_set)


__all__ = ["PublishedInputPreloader"]
