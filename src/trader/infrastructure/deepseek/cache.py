"""Thread-safe bounded in-memory cache for strategy-independent reviews."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from trader.domain.models import DeepSeekReview


@dataclass(frozen=True)
class _CacheEntry:
    review: DeepSeekReview
    expires_at: float


class ReviewCache:
    def __init__(
        self,
        *,
        maximum_entries: int = 2000,
        ttl_seconds: float = 600,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._maximum_entries = max(1, maximum_entries)
        self._ttl_seconds = max(1.0, ttl_seconds)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> DeepSeekReview | None:
        now = self._monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.expires_at <= now:
                self._misses += 1
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return entry.review

    def put(self, key: str, review: DeepSeekReview) -> None:
        with self._lock:
            self._entries[key] = _CacheEntry(review, self._monotonic() + self._ttl_seconds)
            self._entries.move_to_end(key)
            while len(self._entries) > self._maximum_entries:
                self._entries.popitem(last=False)

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "hits": self._hits, "misses": self._misses}


__all__ = ["ReviewCache"]
