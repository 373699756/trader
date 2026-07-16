"""Lifecycle ownership for historical-factor and sentiment refresh workers."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from typing import Literal, Protocol, TypedDict

_LOGGER = logging.getLogger(__name__)

RefreshKind = Literal["history", "sentiment"]
HistoryRefresher = Callable[[Sequence[str]], object]
SentimentScorer = Callable[[str, str], Mapping[str, object]]
ErrorRecorder = Callable[[str], object]
ThreadFactory = Callable[..., threading.Thread]
Clock = Callable[[], float]
CodeNormalizer = Callable[[object], str]


class SentimentCachePort(Protocol):
    ttl_seconds: int

    def get(self) -> object: ...

    def set(self, value: object) -> object: ...


class RefreshDomainStatus(TypedDict):
    active_threads: int
    refreshing_items: int
    success_count: int
    failure_count: int
    last_success_at: str
    last_failure_at: str
    last_error: str
    last_duration_ms: float


class FactorSentimentRefreshStatus(TypedDict):
    stopping: bool
    history: RefreshDomainStatus
    sentiment: RefreshDomainStatus


@dataclass
class _DomainState:
    refreshing: set[str] = field(default_factory=set)
    success_count: int = 0
    failure_count: int = 0
    last_success_ts: float = 0.0
    last_failure_ts: float = 0.0
    last_error: str = ""
    last_duration_ms: float = 0.0


@dataclass(frozen=True)
class _WorkerRegistration:
    kind: RefreshKind
    keys: tuple[str, ...]


class FactorSentimentRefreshService:
    """Own single-flight refresh threads used by candidate enrichment."""

    def __init__(
        self,
        *,
        refresh_history: HistoryRefresher,
        score_sentiment: SentimentScorer,
        sentiment_cache: SentimentCachePort,
        normalize_code: CodeNormalizer,
        record_error: ErrorRecorder | None = None,
        thread_factory: ThreadFactory | None = None,
        wall_clock: Clock = time.time,
        monotonic: Clock = time.monotonic,
    ) -> None:
        self._refresh_history = refresh_history
        self._score_sentiment = score_sentiment
        self._sentiment_cache = sentiment_cache
        self._normalize_code = normalize_code
        self._record_error = record_error
        self._thread_factory = thread_factory or threading.Thread
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._states: dict[RefreshKind, _DomainState] = {
            "history": _DomainState(),
            "sentiment": _DomainState(),
        }
        self._workers: dict[threading.Thread, _WorkerRegistration] = {}
        self._stopping = False
        self._joining = False

    def schedule_history(self, codes: Iterable[object] | None) -> bool:
        normalized_codes = self._normalize_codes(codes)
        if not normalized_codes:
            return False
        with self._lock:
            state = self._states["history"]
            if self._stopping:
                return False
            queued = tuple(code for code in normalized_codes if code not in state.refreshing)
            if not queued:
                return False
            state.refreshing.update(queued)
            work = partial(self._refresh_history_batch, queued)
            return self._launch_worker_locked(
                "history",
                queued,
                work,
                name="history-factor-refresh",
            )

    def sentiment_for_candidates(
        self,
        candidates: Iterable[Mapping[str, object]] | None,
        *,
        limit: int = 30,
    ) -> dict[str, dict[str, object]]:
        items = self._normalize_candidates(candidates, limit=max(0, int(limit)))
        if not items:
            return {}

        with self._lock:
            try:
                entries = self._read_sentiment_entries()
            except Exception as exc:
                _LOGGER.exception("failed to read sentiment refresh cache")
                self._record_failure_locked("sentiment", exc, duration_ms=0.0)
                entries = {}

            lookup: dict[str, dict[str, object]] = {}
            pending: list[tuple[str, str]] = []
            now = self._wall_clock()
            for code, name in items:
                entry = entries.get(code)
                value = entry.get("value") if isinstance(entry, Mapping) else None
                expires_at = self._timestamp(entry.get("expires_at")) if isinstance(entry, Mapping) else 0.0
                if isinstance(value, Mapping):
                    lookup[code] = dict(value)
                    if expires_at <= now:
                        pending.append((code, name))
                    continue
                lookup[code] = self.default_sentiment("舆情刷新中")
                pending.append((code, name))

            self._schedule_sentiment_locked(pending, entries)
            return lookup

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._lock:
            self._stopping = True
            self._joining = True
            workers = tuple(self._workers)

        deadline = self._monotonic() + max(0.0, timeout_seconds)
        for worker in workers:
            if worker is threading.current_thread() or not worker.is_alive():
                continue
            worker.join(max(0.0, deadline - self._monotonic()))

        with self._lock:
            self._joining = False
            self._stopping = any(worker.is_alive() for worker in self._workers)

    def status(self) -> FactorSentimentRefreshStatus:
        with self._lock:
            return {
                "stopping": self._stopping,
                "history": self._domain_status_locked("history"),
                "sentiment": self._domain_status_locked("sentiment"),
            }

    @staticmethod
    def default_sentiment(summary: str = "舆情接口暂不可用") -> dict[str, object]:
        return {
            "score": 50.0,
            "summary": summary,
            "risk_words": [],
            "trigger_words": [],
            "items": [],
        }

    def _schedule_sentiment_locked(
        self,
        candidates: Sequence[tuple[str, str]],
        entries: dict[str, object],
    ) -> bool:
        state = self._states["sentiment"]
        if self._stopping:
            return False
        queued = tuple(item for item in candidates if item[0] not in state.refreshing)
        if not queued:
            return False
        keys = tuple(code for code, _name in queued)
        state.refreshing.update(keys)
        try:
            self._write_sentiment_state(entries)
        except Exception as exc:
            state.refreshing.difference_update(keys)
            self._record_failure_locked("sentiment", exc, duration_ms=0.0)
            _LOGGER.exception("failed to mark sentiment refresh work as running")
            return False
        work = partial(self._refresh_sentiment_batch, queued)
        return self._launch_worker_locked(
            "sentiment",
            keys,
            work,
            name="sentiment-refresh",
        )

    def _launch_worker_locked(
        self,
        kind: RefreshKind,
        keys: tuple[str, ...],
        work: Callable[[], None],
        *,
        name: str,
    ) -> bool:
        worker: threading.Thread | None = None
        try:
            worker = self._thread_factory(
                target=self._run_worker,
                args=(kind, keys, work),
                name=name,
                daemon=True,
            )
            self._workers[worker] = _WorkerRegistration(kind=kind, keys=keys)
            worker.start()
        except Exception as exc:
            if worker is not None:
                self._workers.pop(worker, None)
            self._states[kind].refreshing.difference_update(keys)
            if kind == "sentiment":
                try:
                    self._synchronize_sentiment_refreshing_locked()
                except Exception:
                    _LOGGER.exception("failed to roll back sentiment refresh state")
            self._record_failure_locked(kind, exc, duration_ms=0.0)
            _LOGGER.exception("failed to start %s refresh worker", kind)
            return False
        return True

    def _run_worker(
        self,
        kind: RefreshKind,
        keys: tuple[str, ...],
        work: Callable[[], None],
    ) -> None:
        started_at = self._monotonic()
        error: Exception | None = None
        try:
            work()
        except Exception as exc:
            error = exc
            _LOGGER.exception("%s refresh worker failed", kind)
            self._report_worker_error(kind, exc)
        finally:
            duration_ms = max(0.0, (self._monotonic() - started_at) * 1000.0)
            self._finish_worker(kind, keys, error, duration_ms)

    def _refresh_history_batch(self, codes: Sequence[str]) -> None:
        result = self._refresh_history(codes)
        if not isinstance(result, Mapping):
            return
        failed_count = self._nonnegative_int(result.get("failed"))
        if failed_count <= 0:
            return
        errors = result.get("errors")
        detail = ""
        if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes)) and errors:
            detail = f": {errors[0]}"
        raise RuntimeError(f"{failed_count} historical factor refresh item(s) failed{detail}")

    def _refresh_sentiment_batch(self, candidates: Sequence[tuple[str, str]]) -> None:
        refreshed: dict[str, object] = {}
        failures: list[str] = []
        ttl_seconds = max(30, int(self._sentiment_cache.ttl_seconds or 0))
        expires_at = self._wall_clock() + ttl_seconds
        for code, name in candidates:
            try:
                value = dict(self._score_sentiment(code, name))
            except Exception as exc:
                failures.append(f"{code}: {exc}")
                value = self.default_sentiment()
            refreshed[code] = {
                "value": value,
                "expires_at": expires_at,
            }

        with self._lock:
            entries = self._read_sentiment_entries()
            entries.update(refreshed)
            self._write_sentiment_state(entries)

        if failures:
            raise RuntimeError(f"{len(failures)} sentiment refresh item(s) failed: {failures[0]}")

    def _finish_worker(
        self,
        kind: RefreshKind,
        keys: tuple[str, ...],
        error: Exception | None,
        duration_ms: float,
    ) -> None:
        completion_error = error
        with self._lock:
            self._workers.pop(threading.current_thread(), None)
            self._states[kind].refreshing.difference_update(keys)
            if kind == "sentiment":
                try:
                    self._synchronize_sentiment_refreshing_locked()
                except Exception as exc:
                    _LOGGER.exception("failed to finalize sentiment refresh state")
                    if completion_error is None:
                        completion_error = exc
            if completion_error is None:
                self._record_success_locked(kind, duration_ms)
            else:
                self._record_failure_locked(kind, completion_error, duration_ms)
            if self._stopping and not self._joining and not self._workers:
                self._stopping = False
        if error is None and completion_error is not None:
            self._report_worker_error(kind, completion_error)

    def _read_sentiment_entries(self) -> dict[str, object]:
        cached = self._sentiment_cache.get()
        if not isinstance(cached, Mapping):
            return {}
        entries = cached.get("entries")
        return dict(entries) if isinstance(entries, Mapping) else {}

    def _write_sentiment_state(self, entries: Mapping[str, object]) -> None:
        self._sentiment_cache.set(
            {
                "entries": dict(entries),
                "refreshing": set(self._states["sentiment"].refreshing),
            }
        )

    def _synchronize_sentiment_refreshing_locked(self) -> None:
        entries = self._read_sentiment_entries()
        self._write_sentiment_state(entries)

    def _record_success_locked(self, kind: RefreshKind, duration_ms: float) -> None:
        state = self._states[kind]
        state.success_count += 1
        state.last_success_ts = self._wall_clock()
        state.last_error = ""
        state.last_duration_ms = duration_ms

    def _record_failure_locked(
        self,
        kind: RefreshKind,
        error: Exception,
        duration_ms: float,
    ) -> None:
        state = self._states[kind]
        state.failure_count += 1
        state.last_failure_ts = self._wall_clock()
        state.last_error = str(error)
        state.last_duration_ms = duration_ms

    def _report_worker_error(self, kind: RefreshKind, error: Exception) -> None:
        if self._record_error is None:
            return
        label = "历史因子" if kind == "history" else "舆情"
        try:
            self._record_error(f"后台{label}刷新失败: {error}")
        except Exception:
            _LOGGER.exception("failed to record %s refresh error", kind)

    def _domain_status_locked(self, kind: RefreshKind) -> RefreshDomainStatus:
        state = self._states[kind]
        active_threads = sum(
            worker.is_alive() for worker, registration in self._workers.items() if registration.kind == kind
        )
        return {
            "active_threads": active_threads,
            "refreshing_items": len(state.refreshing),
            "success_count": state.success_count,
            "failure_count": state.failure_count,
            "last_success_at": self._format_timestamp(state.last_success_ts),
            "last_failure_at": self._format_timestamp(state.last_failure_ts),
            "last_error": state.last_error,
            "last_duration_ms": float(state.last_duration_ms),
        }

    def _normalize_codes(self, codes: Iterable[object] | None) -> tuple[str, ...]:
        return tuple(sorted({code for value in codes or () if (code := self._normalize_code(value))}))

    def _normalize_candidates(
        self,
        candidates: Iterable[Mapping[str, object]] | None,
        *,
        limit: int,
    ) -> tuple[tuple[str, str], ...]:
        normalized: dict[str, str] = {}
        for item in candidates or ():
            if len(normalized) >= limit:
                break
            code = self._normalize_code(item.get("code"))
            if code and code not in normalized:
                normalized[code] = str(item.get("name") or "")
        return tuple(normalized.items())

    @staticmethod
    def _timestamp(value: object) -> float:
        if not isinstance(value, (int, float, str)):
            return 0.0
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _nonnegative_int(value: object) -> int:
        if not isinstance(value, (int, float, str)):
            return 0
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _format_timestamp(value: float) -> str:
        if not value:
            return ""
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")


__all__ = [
    "FactorSentimentRefreshService",
    "FactorSentimentRefreshStatus",
    "RefreshDomainStatus",
    "SentimentCachePort",
]
