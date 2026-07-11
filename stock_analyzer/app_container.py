import threading
import time
from datetime import datetime
from typing import Dict

from . import config
from .candidate_pipeline import CandidatePipeline
from .providers import MarketDataProvider, TimedCache
from .recommendation_snapshot import save_recommendation_snapshot
from .stability import TopKDropoutTracker
from .strategy_validation import StrategyValidationStore
from .tomorrow_iteration import TomorrowIterationService
from .validation_cache import ValidationMetricsCache


class AsyncSnapshotWriter:
    """Coalesces recommendation snapshot writes onto one background worker."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._running = False
        self._payload: Dict[str, object] | None = None

    def schedule(self, payload: Dict[str, object]) -> None:
        with self._lock:
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
                save_recommendation_snapshot(self.path, payload)
            except Exception:
                continue


class PayloadCache:
    """Thread-safe in-memory payload cache keyed by route parameters."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[tuple, Dict[str, object]] = {}
        self.refreshing = set()

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
                "payload": payload,
                "snapshot": {
                    "source": source,
                    "stage": stage,
                    "saved_at": saved_at or datetime.now().isoformat(timespec="seconds"),
                    "saved_at_ts": float(saved_at_ts if saved_at_ts is not None else time.time()),
                },
            }
        else:
            cached = {
                "payload": payload,
                "saved_at": saved_at or datetime.now().isoformat(timespec="seconds"),
                "saved_at_ts": float(saved_at_ts if saved_at_ts is not None else time.time()),
                "source": source,
            }
        with self._lock:
            self._entries[key] = cached
        return cached

    def get(self, key: tuple) -> Dict[str, object] | None:
        with self._lock:
            entry = self._entries.get(key)
        return dict(entry) if entry else None

    def mark_refreshing(self, key: tuple) -> bool:
        with self._lock:
            if key in self.refreshing:
                return False
            self.refreshing.add(key)
            return True

    def discard_refreshing(self, key: tuple) -> None:
        with self._lock:
            self.refreshing.discard(key)


class ApplicationContainer:
    """Owns runtime collaborators shared by Flask routes and background workers."""

    def __init__(self) -> None:
        self.provider = MarketDataProvider()
        self.quotes_cache = TimedCache(config.REFRESH_SECONDS)
        self.hot_cache = TimedCache(config.REFRESH_SECONDS * 2)
        self.industry_cache = TimedCache(config.REFRESH_SECONDS * 5)
        self.market_news_cache = TimedCache(config.REFRESH_SECONDS * 3)
        self.market_sentiment_cache = TimedCache(config.REFRESH_SECONDS * 3)
        self.sentiment_cache = TimedCache(config.REFRESH_SECONDS * 5)
        self.factors_cache = TimedCache(config.REFRESH_SECONDS * 30)
        self.recommendations_lock = threading.Lock()
        self.recommendation_cache = PayloadCache()
        self.horizon_cache = PayloadCache()
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
        self.tomorrow_iteration = TomorrowIterationService()

    def cached_metrics(self, strategy_name: str, days: int):
        return self.validation_cache.metrics(strategy_name, days)

    def cached_strategy_validation_summary(self, strategy_name: str, days: int):
        return self.validation_cache.summary(strategy_name, days)

    def invalidate_metrics_cache(self) -> None:
        self.validation_cache.clear()
