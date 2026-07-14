import copy
import logging
import os
import threading
import time
from collections import OrderedDict
from datetime import datetime
from typing import Dict

from . import config
from .candidate_pipeline import CandidatePipeline
from .providers import MarketDataProvider, TimedCache
from .recommendation_snapshot import load_recommendation_snapshot, save_recommendation_snapshot
from .recommendation_freeze import recommendation_is_frozen
from .realtime_schedule import realtime_refresh_profile
from .stability import TopKDropoutTracker
from .strategy_validation import StrategyValidationStore
from .tomorrow_iteration import TomorrowIterationService
from .validation_cache import ValidationMetricsCache

_LOGGER = logging.getLogger(__name__)


class RealtimeMarketScheduler:
    """Single in-process owner for full-market and targeted quote refresh cadence."""

    def __init__(self, container) -> None:
        self.container = container
        self._lock = threading.Lock()
        self._started = False
        self._last_full_started = 0.0
        self._last_full_success = ""
        self._final_full_date = ""
        self._profile = {}

    def start(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self._started = True
        threading.Thread(target=self._run, name="realtime-market-scheduler", daemon=True).start()
        return True

    def status(self) -> Dict[str, object]:
        with self._lock:
            return {
                "started": self._started,
                "profile": dict(self._profile),
                "last_full_success": self._last_full_success,
                "final_full_date": self._final_full_date,
            }

    def _run(self) -> None:
        while True:
            now = datetime.now()
            profile = realtime_refresh_profile(now)
            with self._lock:
                self._profile = dict(profile)
            if profile.get("active"):
                self.container.candidate_pipeline.refresh_recommendation_quote_groups(profile)
                self._schedule_full_market_refresh(profile, now)
            self._accept_completed_full_refresh()
            time.sleep(1.0)

    def _schedule_full_market_refresh(self, profile, now: datetime) -> None:
        interval = profile.get("full_market_seconds")
        if interval is None:
            return
        force_final = bool(profile.get("force_final_full"))
        current_date = now.date().isoformat()
        due = not self._last_full_started or time.monotonic() - self._last_full_started >= float(interval)
        if force_final and self._final_full_date != current_date:
            due = True
        if not due:
            return
        started = self.container.provider.refresh_realtime_quotes_async(force=True)
        if started:
            self._last_full_started = time.monotonic()
            if force_final:
                self._final_full_date = current_date

    def _accept_completed_full_refresh(self) -> None:
        status = self.container.provider.quote_refresh_status()
        success = str(status.get("last_success_at") or "")
        if not success or success == self._last_full_success:
            return
        self._last_full_success = success
        self.container.quotes_cache.clear()
        if not recommendation_is_frozen():
            self.container.recommendation_cache.clear()
            self.container.horizon_cache.clear()


class AsyncSnapshotWriter:
    """Coalesces recommendation snapshot writes onto one background worker."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._running = False
        self._payload: Dict[str, object] | None = None
        self._success_count = 0
        self._failure_count = 0
        self._last_success_ts = 0.0
        self._last_failure_ts = 0.0
        self._last_error = ""
        self._last_duration_ms = 0.0
        self._last_payload_size = 0

    def schedule(self, payload: Dict[str, object]) -> None:
        if recommendation_is_frozen():
            if not self._should_write_frozen_snapshot(payload):
                return
        payload_size = self._estimate_payload_size(payload)
        with self._lock:
            if payload_size > 0:
                self._last_payload_size = max(self._last_payload_size, payload_size)
            self._payload = payload
            if self._running:
                return
            self._running = True
        worker = threading.Thread(
            target=self._worker,
            name="recommendation-snapshot-save",
            daemon=True,
        )
        worker.start()

    def _worker(self) -> None:
        while True:
            with self._lock:
                payload = self._payload
                self._payload = None
                if payload is None:
                    self._running = False
                    return
            try:
                start = time.perf_counter()
                save_recommendation_snapshot(self.path, payload)
                elapsed_ms = max(0.0, (time.perf_counter() - start) * 1000.0)
                with self._lock:
                    self._success_count += 1
                    self._last_success_ts = time.time()
                    self._last_error = ""
                    self._last_duration_ms = elapsed_ms
            except Exception as exc:
                _LOGGER.exception("推荐快照写入失败: %s", exc)
                with self._lock:
                    self._failure_count += 1
                    self._last_failure_ts = time.time()
                    self._last_error = str(exc)

    def _should_write_frozen_snapshot(self, payload: Dict[str, object]) -> bool:
        try:
            if not os.path.exists(self.path):
                return True
            meta = payload.get("meta") if isinstance(payload, dict) else None
            if not isinstance(meta, dict):
                meta = {}
            expected_market = str(meta.get("market_filter") or "")
            expected_top_n = int(meta.get("top_n") or 0)
            max_age_seconds = int(getattr(config, "RECOMMENDATION_SNAPSHOT_MAX_AGE_SECONDS", 300) or 0)
            snapshot = load_recommendation_snapshot(
                self.path,
                max_age_seconds=max_age_seconds,
                expected_market=expected_market,
                expected_top_n=expected_top_n,
            )
            return not bool(snapshot.get("ok"))
        except Exception as exc:
            # If the frozen cache check fails, allow one write attempt to recover.
            _LOGGER.warning("冷启动快照检查失败，改为允许落盘: %s", exc)
            return True

    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "running": self._running,
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

    @staticmethod
    def _estimate_payload_size(payload: Dict[str, object]) -> int:
        try:
            return len(str(payload))
        except Exception:
            return 0


class PayloadCache:
    """Thread-safe in-memory payload cache keyed by route parameters."""

    def __init__(
        self,
        *,
        max_entries: int = 64,
        ttl_seconds: int = 60,
    ) -> None:
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple, Dict[str, object]] = OrderedDict()
        self.refreshing = set()
        self._refreshing: Dict[tuple, float] = {}
        self._max_entries = max(1, int(max_entries))
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._refreshing_ttl = max(1, max(30, self._ttl_seconds * 2))
        self._stats = {
            "hits": 0,
            "misses": 0,
            "expired": 0,
            "evictions": 0,
            "sets": 0,
            "refresh_skips": 0,
            "refreshes_active": 0,
            "memory_bytes": 0,
        }

    @staticmethod
    def _copy_value(value):
        if isinstance(value, dict):
            return copy.deepcopy(value)
        if isinstance(value, list):
            return copy.deepcopy(value)
        return copy.deepcopy(value)

    @staticmethod
    def _estimate_bytes(value) -> int:
        try:
            return len(str(value))
        except Exception:
            return 0

    def _cleanup_expired(self, now: float | None = None) -> None:
        now = float(now if now is not None else time.time())
        expired_keys = [
            key
            for key, entry in list(self._entries.items())
            if float(entry.get("expires_at") or 0.0) and now >= float(entry.get("expires_at") or 0.0)
        ]
        for key in expired_keys:
            self._entries.pop(key, None)
            self._stats["expired"] += 1
        if expired_keys:
            self._stats["memory_bytes"] = self._entry_memory_bytes()

    def _cleanup_refreshing(self, now: float | None = None) -> None:
        now = float(now if now is not None else time.time())
        stale = [key for key, started_at in self._refreshing.items() if now - started_at > self._refreshing_ttl]
        for key in stale:
            self._refreshing.pop(key, None)
            self.refreshing.discard(key)

    def _entry_memory_bytes(self) -> int:
        return sum(self._estimate_bytes(entry.get("value")) for entry in self._entries.values())

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
            self._stats["evictions"] += 1
        self._stats["memory_bytes"] = self._entry_memory_bytes()

    def remember(
        self,
        key: tuple,
        payload: Dict[str, object],
        *,
        source: str = "live",
        stage: str = "ready",
        saved_at: str = "",
        saved_at_ts: float | None = None,
        include_snapshot: bool = True,
    ) -> Dict[str, object]:
        if include_snapshot:
            cached = {
                "payload": self._copy_value(payload),
                "snapshot": {
                    "source": source,
                    "stage": stage,
                    "saved_at": saved_at or datetime.now().isoformat(timespec="seconds"),
                    "saved_at_ts": float(saved_at_ts if saved_at_ts is not None else time.time()),
                },
            }
        else:
            cached = {
                "payload": self._copy_value(payload),
                "saved_at": saved_at or datetime.now().isoformat(timespec="seconds"),
                "saved_at_ts": float(saved_at_ts if saved_at_ts is not None else time.time()),
                "source": source,
            }
        with self._lock:
            now = time.time()
            self._cleanup_expired(now)
            self._entries[key] = {
                "value": self._copy_value(cached),
                "created_at": now,
                "expires_at": now + self._ttl_seconds if self._ttl_seconds > 0 else 0.0,
            }
            self._entries.move_to_end(key, last=True)
            self._evict_if_needed()
            self._stats["sets"] += 1
            self._stats["memory_bytes"] = self._entry_memory_bytes()
        return self._copy_value(cached)

    def get(self, key: tuple) -> Dict[str, object] | None:
        with self._lock:
            self._cleanup_expired()
            entry = self._entries.get(key)
            if entry is None:
                self._stats["misses"] += 1
                return None
            if float(entry.get("expires_at") or 0.0) and time.time() >= float(entry.get("expires_at") or 0.0):
                self._entries.pop(key, None)
                self._stats["misses"] += 1
                self._stats["expired"] += 1
                self._stats["memory_bytes"] = self._entry_memory_bytes()
                return None
            self._stats["hits"] += 1
            self._entries.move_to_end(key, last=True)
            return self._copy_value(entry.get("value"))

    def mark_refreshing(self, key: tuple) -> bool:
        with self._lock:
            now = time.time()
            self._cleanup_refreshing(now)
            self._stats["refreshes_active"] = len(self._refreshing)
            if key in self._refreshing:
                self._stats["refresh_skips"] += 1
                return False
            self._refreshing[key] = now
            self.refreshing.add(key)
            self._stats["refreshes_active"] = len(self._refreshing)
            return True

    def discard_refreshing(self, key: tuple) -> None:
        with self._lock:
            self._cleanup_refreshing()
            self.refreshing.discard(key)
            self._refreshing.pop(key, None)
            self._stats["refreshes_active"] = len(self._refreshing)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.refreshing.clear()
            self._refreshing.clear()
            self._stats["hits"] = 0
            self._stats["misses"] = 0
            self._stats["expired"] = 0
            self._stats["evictions"] = 0
            self._stats["sets"] = 0
            self._stats["refresh_skips"] = 0
            self._stats["refreshes_active"] = 0
            self._stats["memory_bytes"] = 0

    def stats(self) -> Dict[str, object]:
        with self._lock:
            self._cleanup_expired()
            self._cleanup_refreshing()
            return {
                **self._stats,
                "entries": len(self._entries),
                "max_entries": int(self._max_entries),
                "ttl_seconds": int(self._ttl_seconds),
                "refreshing": len(self._refreshing),
            }


class ApplicationContainer:
    """Owns runtime collaborators shared by Flask routes and background workers."""

    def __init__(self) -> None:
        self.provider = MarketDataProvider(web_nonblocking=True)
        self.quotes_cache = TimedCache(
            max(1, int(getattr(config, "QUOTE_CACHE_TTL_SECONDS", config.REFRESH_SECONDS)))
        )
        self.recommendation_quotes_cache = TimedCache(
            max(1, int(getattr(config, "RECOMMENDATION_QUOTE_CACHE_TTL_SECONDS", 15)))
        )
        self.hot_cache = TimedCache(config.REFRESH_SECONDS * 2)
        self.industry_cache = TimedCache(config.REFRESH_SECONDS * 5)
        self.market_news_cache = TimedCache(config.REFRESH_SECONDS * 3)
        self.market_sentiment_cache = TimedCache(config.REFRESH_SECONDS * 3)
        self.sentiment_cache = TimedCache(config.REFRESH_SECONDS * 5)
        self.factors_cache = TimedCache(config.REFRESH_SECONDS * 30)
        self.recommendations_lock = threading.Lock()
        self.recommendation_cache = PayloadCache(
            max_entries=max(1, int(getattr(config, "PAYLOAD_CACHE_MAX_ENTRIES", 48))),
            ttl_seconds=max(1, int(getattr(config, "PAYLOAD_CACHE_TTL_SECONDS", 90))),
        )
        self.horizon_cache = PayloadCache(
            max_entries=max(1, int(getattr(config, "PAYLOAD_CACHE_MAX_ENTRIES", 48)) // 2),
            ttl_seconds=max(1, int(getattr(config, "PAYLOAD_CACHE_TTL_SECONDS", 90))),
        )
        recommendation_limit = max(0, int(getattr(config, "RECOMMENDATION_DISPLAY_LIMIT", 18)))
        self.stability_tracker = TopKDropoutTracker(
            config.STATE_PATH,
            keep_k=max(config.DEFAULT_TOP_N, recommendation_limit),
            buffer_k=max(config.DEFAULT_TOP_N * 2, recommendation_limit * 2),
        )
        self.validation_store = StrategyValidationStore(config.VALIDATION_DB_PATH)
        self.validation_cache = ValidationMetricsCache(self.validation_store)
        self.snapshot_writer = AsyncSnapshotWriter(config.RECOMMENDATION_SNAPSHOT_PATH)
        self.candidate_pipeline = CandidatePipeline(self.provider, self)
        self.realtime_scheduler = RealtimeMarketScheduler(self)
        self.tomorrow_iteration = TomorrowIterationService()

    def cached_metrics(self, strategy_name: str, days: int):
        return self.validation_cache.metrics(strategy_name, days)

    def cached_strategy_validation_summary(self, strategy_name: str, days: int):
        return self.validation_cache.summary(strategy_name, days)

    def invalidate_metrics_cache(self) -> None:
        self.validation_cache.clear()

    def cache_health(self) -> Dict[str, object]:
        return {
            "quotes_cache": self.quotes_cache.stats(),
            "recommendation_quotes_cache": self.recommendation_quotes_cache.stats(),
            "hot_cache": self.hot_cache.stats(),
            "industry_cache": self.industry_cache.stats(),
            "market_news_cache": self.market_news_cache.stats(),
            "market_sentiment_cache": self.market_sentiment_cache.stats(),
            "sentiment_cache": self.sentiment_cache.stats(),
            "factors_cache": self.factors_cache.stats(),
            "recommendation_cache": self.recommendation_cache.stats(),
            "horizon_cache": self.horizon_cache.stats(),
            "realtime_scheduler": self.realtime_scheduler.status(),
            "recommendation_quote_groups": self.candidate_pipeline.recommendation_quote_status(),
        }

    def snapshot_writer_health(self) -> Dict[str, object]:
        return self.snapshot_writer.stats()
