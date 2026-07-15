"""Lifecycle-owned asynchronous recommendation snapshot persistence."""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, cast

from . import config
from .recommendation_freeze import recommendation_is_frozen
from .recommendation_snapshot import load_recommendation_snapshot, save_recommendation_snapshot

_LOGGER = logging.getLogger(__name__)

SnapshotPayload = dict[str, object]
SnapshotSaver = Callable[[str, SnapshotPayload], object]
SnapshotLoader = Callable[..., dict[str, object]]
ThreadFactory = Callable[..., threading.Thread]


class SnapshotWriterConfig(Protocol):
    RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS: int


_DEFAULT_CONFIG = cast(SnapshotWriterConfig, config)


class AsyncSnapshotWriter:
    """Coalesce snapshot writes while owning the worker's complete lifecycle."""

    def __init__(
        self,
        path: str,
        *,
        save_snapshot: SnapshotSaver = save_recommendation_snapshot,
        load_snapshot: SnapshotLoader = load_recommendation_snapshot,
        is_frozen: Callable[[], bool] = recommendation_is_frozen,
        path_exists: Callable[[str], bool] = os.path.exists,
        config_source: SnapshotWriterConfig = _DEFAULT_CONFIG,
        thread_factory: ThreadFactory | None = None,
    ) -> None:
        self.path = path
        self._save_snapshot = save_snapshot
        self._load_snapshot = load_snapshot
        self._is_frozen = is_frozen
        self._path_exists = path_exists
        self._config = config_source
        self._thread_factory = thread_factory or threading.Thread
        self._lock = threading.Lock()
        self._running = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._payload: SnapshotPayload | None = None
        self._success_count = 0
        self._failure_count = 0
        self._last_success_ts = 0.0
        self._last_failure_ts = 0.0
        self._last_error = ""
        self._last_duration_ms = 0.0
        self._last_payload_size = 0

    def schedule(self, payload: SnapshotPayload) -> None:
        try:
            owned_payload = copy.deepcopy(payload)
        except Exception as exc:
            self._record_failure(exc)
            _LOGGER.exception("failed to take ownership of recommendation snapshot payload")
            return

        if self._is_frozen() and not self._should_write_frozen_snapshot(owned_payload):
            return

        payload_size = self._estimate_payload_size(owned_payload)
        with self._lock:
            if self._stopping:
                return
            if payload_size > 0:
                self._last_payload_size = max(self._last_payload_size, payload_size)
            self._payload = owned_payload
            if self._running:
                return
            self._running = True
            try:
                worker = self._thread_factory(
                    target=self._worker,
                    name="recommendation-snapshot-save",
                    daemon=True,
                )
                self._thread = worker
                worker.start()
            except Exception as exc:
                self._thread = None
                self._running = False
                self._record_failure_locked(exc)
                _LOGGER.exception("failed to start recommendation snapshot worker")

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            worker = self._thread
            if worker is None:
                self._running = False
                self._stopping = False
                return

        if worker is not threading.current_thread() and worker.is_alive():
            worker.join(max(0.0, timeout_seconds))

        with self._lock:
            if self._thread is None:
                self._running = False
                self._stopping = False
            elif self._thread is worker and not worker.is_alive():
                self._thread = None
                self._running = False
                self._stopping = False

    def _worker(self) -> None:
        while True:
            with self._lock:
                payload = self._payload
                self._payload = None
                if payload is None:
                    self._finish_worker_locked()
                    return
            try:
                started_at = time.perf_counter()
                self._save_snapshot(self.path, payload)
                elapsed_ms = max(0.0, (time.perf_counter() - started_at) * 1000.0)
                with self._lock:
                    self._success_count += 1
                    self._last_success_ts = time.time()
                    self._last_error = ""
                    self._last_duration_ms = elapsed_ms
            except Exception as exc:
                _LOGGER.exception("recommendation snapshot write failed: %s", exc)
                self._record_failure(exc)

    def _finish_worker_locked(self) -> None:
        self._running = False
        self._stopping = False
        self._thread = None

    def _should_write_frozen_snapshot(self, payload: SnapshotPayload) -> bool:
        try:
            if not self._path_exists(self.path):
                return True
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if not isinstance(meta, dict):
                meta = {}
            expected_market = str(meta.get("market_filter") or "")
            expected_top_n = int(meta.get("top_n") or 0)
            max_age_seconds = int(self._config.RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS or 0)
            snapshot = self._load_snapshot(
                self.path,
                max_age_seconds=max_age_seconds,
                expected_market=expected_market,
                expected_top_n=expected_top_n,
            )
            return not bool(snapshot.get("ok"))
        except Exception as exc:
            _LOGGER.warning("frozen snapshot check failed; allowing a recovery write: %s", exc)
            return True

    def stats(self) -> dict[str, object]:
        with self._lock:
            return {
                "running": self._running,
                "stopping": self._stopping,
                "pending": self._payload is not None,
                "success_count": int(self._success_count),
                "failure_count": int(self._failure_count),
                "last_success_at": (
                    datetime.fromtimestamp(self._last_success_ts).isoformat(timespec="seconds")
                    if self._last_success_ts
                    else ""
                ),
                "last_failure_at": (
                    datetime.fromtimestamp(self._last_failure_ts).isoformat(timespec="seconds")
                    if self._last_failure_ts
                    else ""
                ),
                "last_error": str(self._last_error),
                "last_duration_ms": float(self._last_duration_ms),
                "last_payload_size": int(self._last_payload_size),
            }

    def _record_failure(self, exc: Exception) -> None:
        with self._lock:
            self._record_failure_locked(exc)

    def _record_failure_locked(self, exc: Exception) -> None:
        self._failure_count += 1
        self._last_failure_ts = time.time()
        self._last_error = str(exc)

    @staticmethod
    def _estimate_payload_size(payload: SnapshotPayload) -> int:
        try:
            return len(str(payload))
        except Exception:
            return 0


__all__ = ["AsyncSnapshotWriter", "SnapshotWriterConfig"]
