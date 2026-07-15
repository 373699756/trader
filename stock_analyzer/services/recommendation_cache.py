from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import Protocol, TypedDict

from ..app_response_support import response_payload
from ..recommendation_snapshot import load_recommendation_snapshot

Payload = dict[str, object]
RecommendationCacheKey = tuple[int, str]
HorizonCacheKey = tuple[str, int, str]
CacheKey = RecommendationCacheKey | HorizonCacheKey

_LOGGER = logging.getLogger(__name__)


class PayloadCachePort(Protocol):
    def remember(
        self,
        key: CacheKey,
        payload: Payload,
        *,
        source: str = "live",
        stage: str = "ready",
        saved_at: str = "",
        saved_at_ts: float | None = None,
        include_snapshot: bool = True,
    ) -> Payload: ...

    def get(self, key: CacheKey) -> Payload | None: ...

    def mark_refreshing(self, key: CacheKey) -> bool: ...

    def discard_refreshing(self, key: CacheKey) -> None: ...


class RefreshWorkerStatus(TypedDict):
    active_threads: int
    stopping: bool
    success_count: int
    failure_count: int
    last_error: str


def _as_payload(value: object) -> Payload:
    return dict(value) if isinstance(value, dict) else {}


def _as_float(value: object, default: float = 0.0) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            pass
    return default


class RecommendationSnapshotService:
    """Owns recommendation/horizon cache keys, cache writes, and disk snapshot reads."""

    def __init__(
        self,
        recommendation_cache: PayloadCachePort,
        horizon_cache: PayloadCachePort,
        *,
        is_frozen: Callable[[], bool],
        overlay_live_quotes: Callable[[Payload], Payload],
        snapshot_path: str,
        snapshot_max_age_seconds: int,
        snapshot_loader: Callable[..., Payload] = load_recommendation_snapshot,
    ) -> None:
        self.recommendation_cache = recommendation_cache
        self.horizon_cache = horizon_cache
        self._is_frozen = is_frozen
        self._overlay_live_quotes = overlay_live_quotes
        self._snapshot_path = snapshot_path
        self._snapshot_max_age_seconds = int(snapshot_max_age_seconds)
        self._snapshot_loader = snapshot_loader

    @staticmethod
    def recommendation_cache_key(top_n: int, market: str) -> RecommendationCacheKey:
        return int(top_n), str(market)

    @staticmethod
    def horizon_cache_key(strategy: str, top_n: int, market: str) -> HorizonCacheKey:
        return str(strategy), int(top_n), str(market)

    def remember_recommendation_payload(
        self,
        top_n: int,
        market: str,
        payload: Payload,
        *,
        source: str,
        stage: str = "ready",
        saved_at: str = "",
        saved_at_ts: float | None = None,
        skip_frozen_lookup: bool = False,
    ) -> Payload:
        if self._is_frozen() and not skip_frozen_lookup:
            frozen = self.cached_recommendation_entry(top_n, market) or self.snapshot_entry(top_n, market)
            if frozen is not None:
                return frozen
        resolved_saved_at = saved_at or datetime.now().isoformat(timespec="seconds")
        resolved_saved_at_ts = float(saved_at_ts if saved_at_ts is not None else time.time())
        return self.recommendation_cache.remember(
            self.recommendation_cache_key(top_n, market),
            payload,
            source=source,
            stage=stage,
            saved_at=resolved_saved_at,
            saved_at_ts=resolved_saved_at_ts,
            include_snapshot=True,
        )

    def cached_recommendation_entry(self, top_n: int, market: str) -> Payload | None:
        return self.recommendation_cache.get(self.recommendation_cache_key(top_n, market))

    def remember_horizon_payload(
        self,
        strategy: str,
        top_n: int,
        market: str,
        payload: Payload,
        *,
        saved_at: str = "",
        saved_at_ts: float | None = None,
        source: str = "live",
    ) -> Payload:
        if self._is_frozen():
            frozen = self.cached_horizon_entry(strategy, top_n, market)
            if frozen is not None:
                return frozen
        return self.horizon_cache.remember(
            self.horizon_cache_key(strategy, top_n, market),
            payload,
            source=source,
            saved_at=saved_at,
            saved_at_ts=saved_at_ts,
            include_snapshot=False,
        )

    def cached_horizon_entry(self, strategy: str, top_n: int, market: str) -> Payload | None:
        return self.horizon_cache.get(self.horizon_cache_key(strategy, top_n, market))

    def snapshot_entry(self, top_n: int, market: str) -> Payload | None:
        snapshot = self._snapshot_loader(
            self._snapshot_path,
            max_age_seconds=self._snapshot_max_age_seconds,
            expected_market=market,
            expected_top_n=top_n,
        )
        if not snapshot.get("ok"):
            return None
        return self.remember_recommendation_payload(
            top_n,
            market,
            _as_payload(snapshot.get("payload")),
            source="disk_snapshot",
            stage="ready",
            saved_at=str(snapshot.get("saved_at") or ""),
            saved_at_ts=time.time() - _as_float(snapshot.get("age_seconds")),
            skip_frozen_lookup=True,
        )

    @staticmethod
    def snapshot_info(entry: Payload) -> Payload:
        snapshot = _as_payload(entry.get("snapshot"))
        saved_at_ts = _as_float(snapshot.get("saved_at_ts"))
        snapshot["age_seconds"] = round(max(0.0, time.time() - saved_at_ts), 2) if saved_at_ts else None
        return snapshot

    def serve_recommendation_payload(self, entry: Payload) -> Payload:
        payload = _as_payload(entry.get("payload"))
        payload["snapshot"] = self.snapshot_info(entry)
        return self._overlay_live_quotes(payload)


class RecommendationRefreshService:
    """Owns background refresh single-flight scheduling for recommendation payloads."""

    def __init__(
        self,
        snapshots: RecommendationSnapshotService,
        *,
        refresh_quotes: Callable[[], object],
        build_recommendations: Callable[..., tuple[Payload, int]],
        build_horizon: Callable[[str, int, str], Payload],
        provider_health: Callable[[], Payload],
        research_disclaimer: Callable[[], str],
        is_frozen: Callable[[], bool],
        thread_factory: Callable[..., threading.Thread] | None = None,
    ) -> None:
        self.snapshots = snapshots
        self._refresh_quotes = refresh_quotes
        self._build_recommendations = build_recommendations
        self._build_horizon = build_horizon
        self._provider_health = provider_health
        self._research_disclaimer = research_disclaimer
        self._is_frozen = is_frozen
        self._thread_factory = thread_factory or threading.Thread
        self._worker_lock = threading.Lock()
        self._workers: set[threading.Thread] = set()
        self._stopping = False
        self._success_count = 0
        self._failure_count = 0
        self._last_error = ""

    def refresh_recommendation_cache(self, top_n: int, market: str) -> None:
        key = self.snapshots.recommendation_cache_key(top_n, market)
        try:
            if self._is_frozen():
                return
            self._refresh_quotes()
            self._build_recommendations(top_n, market, include_deepseek=True)
        finally:
            self.snapshots.recommendation_cache.discard_refreshing(key)

    def _run_tracked(self, target: Callable[[], None], worker_name: str) -> None:
        try:
            target()
        except Exception as exc:
            _LOGGER.exception("recommendation refresh worker failed: %s", worker_name)
            with self._worker_lock:
                self._failure_count += 1
                self._last_error = str(exc)
        else:
            with self._worker_lock:
                self._success_count += 1
                self._last_error = ""
        finally:
            with self._worker_lock:
                self._workers.discard(threading.current_thread())
                if not self._workers:
                    self._stopping = False

    def _start_worker(self, target: Callable[[], None], name: str) -> bool:
        with self._worker_lock:
            if self._stopping:
                return False
            worker = self._thread_factory(
                target=self._run_tracked,
                args=(target, name),
                name=name,
                daemon=True,
            )
            self._workers.add(worker)
            try:
                worker.start()
            except Exception:
                self._workers.discard(worker)
                raise
        return True

    def stop(self, timeout_seconds: float = 5.0) -> None:
        with self._worker_lock:
            self._stopping = True
            workers = list(self._workers)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        for worker in workers:
            if worker is threading.current_thread():
                continue
            worker.join(max(0.0, deadline - time.monotonic()))
        with self._worker_lock:
            self._workers = {worker for worker in workers if worker.is_alive()}
            self._stopping = bool(self._workers)

    def status(self) -> RefreshWorkerStatus:
        with self._worker_lock:
            return {
                "active_threads": sum(worker.is_alive() for worker in self._workers),
                "stopping": self._stopping,
                "success_count": self._success_count,
                "failure_count": self._failure_count,
                "last_error": self._last_error,
            }

    def schedule_recommendation_refresh(self, top_n: int, market: str) -> bool:
        if self._is_frozen():
            return False
        key = self.snapshots.recommendation_cache_key(top_n, market)
        if not self.snapshots.recommendation_cache.mark_refreshing(key):
            return False
        try:
            started = self._start_worker(
                partial(self.refresh_recommendation_cache, top_n, market),
                f"recommendation-refresh-{market}-{top_n}",
            )
        except Exception:
            self.snapshots.recommendation_cache.discard_refreshing(key)
            raise
        if not started:
            self.snapshots.recommendation_cache.discard_refreshing(key)
        return started

    def refresh_horizon_cache(self, strategy: str, top_n: int, market: str) -> None:
        key = self.snapshots.horizon_cache_key(strategy, top_n, market)
        try:
            if self._is_frozen():
                return
            self._refresh_quotes()
            self._build_horizon(strategy, top_n, market)
        except Exception as exc:
            payload = response_payload(
                self._provider_health,
                self._research_disclaimer,
                ok=False,
                include_disclaimer=True,
                error=str(exc),
                data=[],
                meta={
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "candidate_count": 0,
                    "display_count": 0,
                    "display_limit": top_n,
                    "top_n": top_n,
                    "market_filter": market,
                    "strategy_label": "明日优先" if strategy == "tomorrow_picks" else "2-5日持有",
                    "strategy": "实时行情刷新失败",
                    "fallback": "live_refresh_failed",
                },
            )
            self.snapshots.remember_horizon_payload(strategy, top_n, market, payload, source="live_refresh_failed")
        finally:
            self.snapshots.horizon_cache.discard_refreshing(key)

    def schedule_horizon_refresh(self, strategy: str, top_n: int, market: str) -> bool:
        if self._is_frozen():
            return False
        key = self.snapshots.horizon_cache_key(strategy, top_n, market)
        if not self.snapshots.horizon_cache.mark_refreshing(key):
            return False
        try:
            started = self._start_worker(
                partial(self.refresh_horizon_cache, strategy, top_n, market),
                f"horizon-refresh-{strategy}-{market}-{top_n}",
            )
        except Exception:
            self.snapshots.horizon_cache.discard_refreshing(key)
            raise
        if not started:
            self.snapshots.horizon_cache.discard_refreshing(key)
        return started


__all__ = [
    "CacheKey",
    "HorizonCacheKey",
    "Payload",
    "PayloadCachePort",
    "RecommendationCacheKey",
    "RecommendationRefreshService",
    "RecommendationSnapshotService",
    "RefreshWorkerStatus",
]
