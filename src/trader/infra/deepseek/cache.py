"""Thread-safe bounded caches for raw and strategy-classified reviews."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.cache import BoundedCache, CacheIdentity, CacheIdentitySpec, build_cache_identity
from trader.domain.market.models import FeatureSnapshot
from trader.domain.review.models import DeepSeekReview


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


@dataclass(frozen=True)
class _SharedRawCacheValue:
    review: DeepSeekReview
    price: float | None
    volume_ratio: float | None


class _ReviewCacheOptions(TypedDict, total=False):
    maximum_entries: int
    ttl_seconds: float
    monotonic: Callable[[], float]
    shared_cache: BoundedCache[object] | None
    config_version: str
    seen_capacity: int


class ReviewCache:
    def __init__(
        self,
        **options: Unpack[_ReviewCacheOptions],
    ) -> None:
        maximum_entries = options.get("maximum_entries", 2000)
        ttl_seconds = options.get("ttl_seconds", 600)
        monotonic = options.get("monotonic", time.monotonic)
        shared_cache = options.get("shared_cache")
        config_version = options.get("config_version", "component-default")
        seen_capacity = options.get("seen_capacity", 6000)
        self._maximum_entries = max(1, maximum_entries)
        self._ttl_seconds = max(1.0, ttl_seconds)
        self._monotonic = monotonic
        self._shared_cache = shared_cache
        self._config_version = config_version
        self._seen_capacity = max(1, seen_capacity)
        self._lock = threading.Lock()
        self._raw_entries: OrderedDict[str, _RawCacheEntry] = OrderedDict()
        self._fusion_entries: OrderedDict[str, _FusionCacheEntry] = OrderedDict()
        self._seen_codes: OrderedDict[str, None] = OrderedDict()
        self._seen_trade_date = ""
        self._raw_hits = 0
        self._fusion_hits = 0
        self._misses = 0

    def get_raw(self, key: str, candidate: FeatureSnapshot) -> DeepSeekReview | None:
        if self._shared_cache is not None:
            lookup = self._shared_cache.get(self._raw_identity(key, candidate))
            value = lookup.value if lookup is not None else None
            if (
                lookup is None
                or lookup.state == "degraded"
                or not isinstance(value, _SharedRawCacheValue)
                or _shared_quote_changed(value, candidate)
            ):
                with self._lock:
                    self._misses += 1
                return None
            with self._lock:
                self._raw_hits += 1
            return value.review
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
        trade_date = _candidate_trade_date(candidate)
        if self._shared_cache is not None:
            self._shared_cache.put(
                self._seen_identity(candidate.quote.code, trade_date),
                True,
                data_version=key,
                source_time=review.completed_at,
            )
        else:
            with self._lock:
                self._mark_seen(candidate.quote.code, trade_date)
        if self._shared_cache is not None:
            self._shared_cache.put(
                self._raw_identity(key, candidate),
                _SharedRawCacheValue(
                    review=review,
                    price=_finite(candidate.quote.price),
                    volume_ratio=_finite(candidate.quote.volume_ratio),
                ),
                data_version=key,
                source_time=review.completed_at,
            )
            return
        with self._lock:
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
        if self._shared_cache is not None:
            lookup = self._shared_cache.get(self._fusion_identity(key))
            value = lookup.value if lookup is not None else None
            if lookup is None or lookup.state == "degraded" or not isinstance(value, DeepSeekReview):
                with self._lock:
                    self._misses += 1
                return None
            with self._lock:
                self._fusion_hits += 1
            return value
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
        if self._shared_cache is not None:
            self._shared_cache.put(
                self._fusion_identity(key),
                review,
                data_version=key,
                source_time=review.completed_at,
            )
            return
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
            shared_raw, shared_fusion = self._shared_entry_counts()
            return {
                "entries": shared_raw + shared_fusion,
                "raw_entries": shared_raw,
                "fusion_entries": shared_fusion,
                "seen_codes": len(self._seen_codes),
                "hits": self._raw_hits + self._fusion_hits,
                "raw_hits": self._raw_hits,
                "fusion_hits": self._fusion_hits,
                "misses": self._misses,
            }

    def has_seen(self, code: str, trade_date: str | None = None) -> bool:
        if self._shared_cache is not None and trade_date is not None:
            lookup = self._shared_cache.get(self._seen_identity(code, trade_date))
            return lookup is not None and lookup.value is True and lookup.state != "degraded"
        with self._lock:
            if trade_date is not None and trade_date != self._seen_trade_date:
                return False
            return code in self._seen_codes

    def _mark_seen(self, code: str, trade_date: str) -> None:
        if trade_date != self._seen_trade_date:
            self._seen_codes.clear()
            self._seen_trade_date = trade_date
        self._seen_codes[code] = None
        self._seen_codes.move_to_end(code)
        while len(self._seen_codes) > self._seen_capacity:
            self._seen_codes.popitem(last=False)

    def _raw_identity(self, key: str, candidate: FeatureSnapshot) -> CacheIdentity:
        return build_cache_identity(
            CacheIdentitySpec(
                dataset="raw_deepseek_review",
                source="deepseek:raw",
                subject_key=candidate.quote.code,
                request={"raw_key": key},
                trade_date=_candidate_trade_date(candidate),
                phase="review",
                source_contract_version="deepseek_review_v17",
                config_version=self._config_version,
                schema_version="deepseek_cache_v17",
            )
        )

    def _fusion_identity(self, key: str) -> CacheIdentity:
        return build_cache_identity(
            CacheIdentitySpec(
                dataset="strategy_deepseek_review",
                source="deepseek:fusion",
                subject_key=key[:24],
                request={"strategy_key": key},
                trade_date="embedded",
                phase="review",
                source_contract_version="deepseek_review_v17",
                config_version=self._config_version,
                schema_version="deepseek_cache_v17",
            )
        )

    def _seen_identity(self, code: str, trade_date: str) -> CacheIdentity:
        return build_cache_identity(
            CacheIdentitySpec(
                dataset="deepseek_seen_codes",
                source="deepseek:seen",
                subject_key=code,
                request={"code": code},
                trade_date=trade_date,
                phase="review",
                source_contract_version="deepseek_review_v17",
                config_version=self._config_version,
                schema_version="deepseek_cache_v17",
            )
        )

    def _shared_entry_counts(self) -> tuple[int, int]:
        if self._shared_cache is None:
            return len(self._raw_entries), len(self._fusion_entries)
        status = self._shared_cache.status()
        return (
            _dataset_entries(status, "raw_deepseek_review"),
            _dataset_entries(status, "strategy_deepseek_review"),
        )


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


def _shared_quote_changed(entry: _SharedRawCacheValue, candidate: FeatureSnapshot) -> bool:
    fallback = _RawCacheEntry(entry.review, 0.0, entry.price, entry.volume_ratio)
    return _quote_changed(fallback, candidate)


def _candidate_trade_date(candidate: FeatureSnapshot) -> str:
    return candidate.quote.source_time.date().isoformat()


def _dataset_entries(status: object, dataset: str) -> int:
    if not isinstance(status, dict):
        return 0
    sources = status.get(dataset)
    if not isinstance(sources, dict):
        return 0
    total = 0
    for values in sources.values():
        if isinstance(values, dict):
            count = values.get("entries")
            if isinstance(count, int) and not isinstance(count, bool):
                total += count
    return total


def _finite(value: float | None) -> float | None:
    if value is None:
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


__all__ = ["ReviewCache"]
