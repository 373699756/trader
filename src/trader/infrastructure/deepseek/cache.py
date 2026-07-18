"""Thread-safe bounded caches for raw and strategy-classified reviews."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal

from trader.domain.models import DeepSeekReview, FeatureSnapshot


@dataclass(frozen=True)
class _RawCacheEntry:
    review: DeepSeekReview
    expires_at: float
    price: float | None
    volume_ratio: float | None


@dataclass(frozen=True)
class _FusionCacheEntry:
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
        self._raw_entries: OrderedDict[str, _RawCacheEntry] = OrderedDict()
        self._fusion_entries: OrderedDict[str, _FusionCacheEntry] = OrderedDict()
        self._seen_codes: set[str] = set()
        self._raw_hits = 0
        self._fusion_hits = 0
        self._misses = 0

    def get_raw(self, key: str, candidate: FeatureSnapshot) -> DeepSeekReview | None:
        now = self._monotonic()
        with self._lock:
            entry = self._raw_entries.get(key)
            if entry is None or entry.expires_at <= now or _quote_changed(entry, candidate):
                self._misses += 1
                self._raw_entries.pop(key, None)
                return None
            self._raw_entries.move_to_end(key)
            self._raw_hits += 1
            return entry.review

    def put_raw(self, key: str, candidate: FeatureSnapshot, review: DeepSeekReview) -> None:
        with self._lock:
            self._seen_codes.add(candidate.quote.code)
            self._raw_entries[key] = _RawCacheEntry(
                review=review,
                expires_at=self._monotonic() + self._ttl_seconds,
                price=_finite(candidate.quote.price),
                volume_ratio=_finite(candidate.quote.volume_ratio),
            )
            self._raw_entries.move_to_end(key)
            while len(self._raw_entries) > self._maximum_entries:
                self._raw_entries.popitem(last=False)

    def get_fusion(self, key: str) -> DeepSeekReview | None:
        now = self._monotonic()
        with self._lock:
            entry = self._fusion_entries.get(key)
            if entry is None or entry.expires_at <= now:
                self._misses += 1
                self._fusion_entries.pop(key, None)
                return None
            self._fusion_entries.move_to_end(key)
            self._fusion_hits += 1
            return entry.review

    def put_fusion(self, key: str, review: DeepSeekReview) -> None:
        with self._lock:
            self._fusion_entries[key] = _FusionCacheEntry(
                review=review,
                expires_at=self._monotonic() + self._ttl_seconds,
            )
            self._fusion_entries.move_to_end(key)
            while len(self._fusion_entries) > self._maximum_entries:
                self._fusion_entries.popitem(last=False)

    def status(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._raw_entries) + len(self._fusion_entries),
                "raw_entries": len(self._raw_entries),
                "fusion_entries": len(self._fusion_entries),
                "seen_codes": len(self._seen_codes),
                "hits": self._raw_hits + self._fusion_hits,
                "raw_hits": self._raw_hits,
                "fusion_hits": self._fusion_hits,
                "misses": self._misses,
            }

    def has_seen(self, code: str) -> bool:
        with self._lock:
            return code in self._seen_codes


def _quote_changed(entry: _RawCacheEntry, candidate: FeatureSnapshot) -> bool:
    price = _finite(candidate.quote.price)
    volume_ratio = _finite(candidate.quote.volume_ratio)
    if entry.price is None or price is None:
        price_changed = entry.price != price
    else:
        stored_price = Decimal(str(entry.price))
        current_price = Decimal(str(price))
        price_changed = abs(current_price / stored_price - Decimal(1)) >= Decimal("0.01")
    if entry.volume_ratio is None or volume_ratio is None:
        volume_changed = entry.volume_ratio != volume_ratio
    else:
        volume_changed = abs(Decimal(str(volume_ratio)) - Decimal(str(entry.volume_ratio))) >= Decimal("0.3")
    return price_changed or volume_changed


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


__all__ = ["ReviewCache"]
