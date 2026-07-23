"""Thread-safe bounded LRU cache with negative caching and single-flight."""

from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Generic, TypeVar, cast

from trader.application.cache import (
    CacheDatasetPolicy,
    CacheIdentity,
    CacheLookup,
    CachePolicy,
    CacheStats,
    canonical_json_bytes,
    freeze_cache_value,
)

_T = TypeVar("_T")


@dataclass(frozen=True)
class CacheEntry(Generic[_T]):
    value: _T | None
    data_version: str | None
    source_time: datetime | None
    error_code: str | None
    inserted_at: float
    negative_inserted_at: float | None
    estimated_bytes: int
    value_estimated_bytes: int


@dataclass
class _MutableStats:
    hit: int = 0
    miss: int = 0
    refresh_due_hit: int = 0
    stale_hit: int = 0
    degraded_hit: int = 0
    negative_hit: int = 0
    refresh: int = 0
    eviction: int = 0
    load_error: int = 0


class BoundedLruCache(Generic[_T]):
    def __init__(
        self,
        policy: CachePolicy,
        *,
        cadence_seconds: Mapping[str, Mapping[str, float]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._policy = policy
        self._cadence_seconds = {task: dict(phases) for task, phases in (cadence_seconds or {}).items()}
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._entries: OrderedDict[CacheIdentity, CacheEntry[_T]] = OrderedDict()
        self._inflight: dict[CacheIdentity, Future[_T]] = {}
        self._inflight_scope_entries: dict[tuple[str, str], int] = {}
        self._stats: dict[tuple[str, str], _MutableStats] = {}
        self._scope_entries: dict[tuple[str, str], int] = {}
        self._scope_bytes: dict[tuple[str, str], int] = {}
        self._group_bytes = {name: 0 for name in policy.groups}
        self._total_bytes = 0
        self._stopped = False

    def get(self, identity: CacheIdentity) -> CacheLookup[_T] | None:
        now = self._monotonic()
        with self._lock:
            self._dataset_policy(identity)
            stats = self._stats_for(identity)
            entry = self._entries.get(identity)
            if entry is None:
                stats.miss += 1
                return None
            elapsed = max(0.0, now - entry.inserted_at)
            policy = self._policy.datasets[identity.dataset]
            if entry.error_code is not None:
                negative_elapsed = max(0.0, now - (entry.negative_inserted_at or entry.inserted_at))
                if negative_elapsed > policy.negative_ttl_seconds:
                    if entry.value is None:
                        self._remove_locked(identity)
                        stats.miss += 1
                        return None
                    entry = self._clear_negative_locked(identity, entry, now)
                    elapsed = max(0.0, now - entry.inserted_at)
                else:
                    self._entries.move_to_end(identity)
                    stats.hit += 1
                    stats.negative_hit += 1
                    if entry.value is None:
                        return CacheLookup(None, "negative", None, None, entry.error_code, True)
                    state = self._value_state(identity, entry, elapsed)
                    self._record_value_state(stats, state)
                    return CacheLookup(
                        entry.value,
                        state,
                        entry.data_version,
                        entry.source_time,
                        entry.error_code,
                        True,
                    )

            state = self._value_state(identity, entry, elapsed)
            self._entries.move_to_end(identity)
            stats.hit += 1
            self._record_value_state(stats, state)
            return CacheLookup(entry.value, state, entry.data_version, entry.source_time)

    def put(
        self,
        identity: CacheIdentity,
        value: _T,
        *,
        data_version: str,
        source_time: datetime,
    ) -> bool:
        if not data_version.strip():
            raise ValueError("cache data_version must not be empty")
        _require_aware(source_time, "cache source_time")
        frozen = freeze_cache_value(value)
        estimated_bytes = self._estimate_entry(identity, frozen, data_version, source_time, None)
        entry = CacheEntry(
            frozen,
            data_version,
            source_time,
            None,
            self._monotonic(),
            None,
            estimated_bytes,
            estimated_bytes,
        )
        return self._store(identity, entry)

    def put_negative(self, identity: CacheIdentity, *, error_code: str) -> bool:
        normalized = error_code.strip()
        if not normalized:
            raise ValueError("negative cache error code must not be empty")
        now = self._monotonic()
        with self._lock:
            current = self._entries.get(identity)
            if current is not None and current.value is not None:
                estimated_bytes = self._estimate_entry(
                    identity,
                    current.value,
                    current.data_version,
                    current.source_time,
                    normalized,
                )
                entry = CacheEntry(
                    current.value,
                    current.data_version,
                    current.source_time,
                    normalized,
                    current.inserted_at,
                    now,
                    estimated_bytes,
                    current.value_estimated_bytes,
                )
            else:
                estimated_bytes = self._estimate_entry(identity, None, None, None, normalized)
                entry = CacheEntry(None, None, None, normalized, now, now, estimated_bytes, estimated_bytes)
            return self._store(identity, entry)

    def coalesce(self, identity: CacheIdentity, loader: Callable[[], _T]) -> _T:
        with self._lock:
            future, owner = self._claim_inflight(identity)
        if not owner:
            return future.result()
        try:
            result = loader()
        except BaseException as exc:
            with self._lock:
                self._stats_for(identity).load_error += 1
            if future.done():
                return future.result()
            future.set_exception(exc)
            raise
        else:
            if future.done():
                return future.result()
            future.set_result(result)
            return result
        finally:
            with self._condition:
                removed = self._inflight.pop(identity, None)
                if removed is future:
                    scope = (identity.dataset, identity.source)
                    remaining = self._inflight_scope_entries.get(scope, 0) - 1
                    if remaining > 0:
                        self._inflight_scope_entries[scope] = remaining
                    else:
                        self._inflight_scope_entries.pop(scope, None)
                self._condition.notify_all()

    def _claim_inflight(self, identity: CacheIdentity) -> tuple[Future[_T], bool]:
        self._dataset_policy(identity)
        if self._stopped:
            raise RuntimeError("cache is stopped")
        current = self._inflight.get(identity)
        if current is not None:
            return current, False
        policy = self._policy.datasets[identity.dataset]
        scope = (identity.dataset, identity.source)
        if self._inflight_scope_entries.get(scope, 0) >= policy.capacity:
            raise RuntimeError("cache in-flight registry is full")
        future: Future[_T] = Future()
        self._inflight[identity] = future
        self._inflight_scope_entries[scope] = self._inflight_scope_entries.get(scope, 0) + 1
        self._stats_for(identity).refresh += 1
        return future, True

    def is_actionable(self, identity: CacheIdentity, source_time: datetime) -> bool:
        _require_aware(source_time, "cache source_time")
        with self._lock:
            self._dataset_policy(identity)
            _refresh_ttl, action_age = self._timing(identity)
            business_age = self._business_age(source_time)
            return business_age is not None and business_age <= action_age

    def status(self) -> dict[str, dict[str, dict[str, object]]]:
        with self._lock:
            scopes = set(self._stats)
            scopes.update(self._scope_entries)
            result: dict[str, dict[str, dict[str, object]]] = {}
            for dataset, source in sorted(scopes):
                stats = self._stats.get((dataset, source), _MutableStats())
                scope = (dataset, source)
                snapshot = CacheStats(
                    entries=self._scope_entries.get(scope, 0),
                    capacity=self._policy.datasets[dataset].capacity,
                    hit=stats.hit,
                    miss=stats.miss,
                    refresh_due_hit=stats.refresh_due_hit,
                    stale_hit=stats.stale_hit,
                    degraded_hit=stats.degraded_hit,
                    negative_hit=stats.negative_hit,
                    refresh=stats.refresh,
                    eviction=stats.eviction,
                    load_error=stats.load_error,
                    estimated_bytes=self._scope_bytes.get(scope, 0),
                )
                result.setdefault(dataset, {})[source] = {
                    "entries": snapshot.entries,
                    "capacity": snapshot.capacity,
                    "hit": snapshot.hit,
                    "miss": snapshot.miss,
                    "refresh_due_hit": snapshot.refresh_due_hit,
                    "stale_hit": snapshot.stale_hit,
                    "degraded_hit": snapshot.degraded_hit,
                    "negative_hit": snapshot.negative_hit,
                    "refresh": snapshot.refresh,
                    "eviction": snapshot.eviction,
                    "load_error": snapshot.load_error,
                    "hit_rate": round(snapshot.hit_rate, 6),
                    "estimated_bytes": snapshot.estimated_bytes,
                }
            return result

    def stop(self, *, wait: bool = True, timeout_seconds: float | None = None) -> None:
        deadline = None if timeout_seconds is None else time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            self._stopped = True
            while wait and self._inflight:
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            remaining_futures = tuple(self._inflight.values())
            self._inflight.clear()
            self._inflight_scope_entries.clear()
            for future in remaining_futures:
                if not future.done():
                    future.set_exception(RuntimeError("cache stopped during load"))
            self._condition.notify_all()

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._inflight)

    def _store(self, identity: CacheIdentity, entry: CacheEntry[_T]) -> bool:
        with self._lock:
            if self._stopped:
                return False
            policy = self._dataset_policy(identity)
            group_limit = self._policy.groups[policy.group].max_bytes
            if entry.estimated_bytes > group_limit or entry.estimated_bytes > self._policy.total_bytes:
                self._stats_for(identity).load_error += 1
                return False
            current = self._entries.get(identity)
            if (
                current is not None
                and current.value is not None
                and entry.value is not None
                and current.source_time is not None
                and entry.source_time is not None
                and current.data_version is not None
                and entry.data_version is not None
                and (entry.source_time, entry.data_version) < (current.source_time, current.data_version)
            ):
                return False
            self._remove_locked(identity)
            self._evict_scope_locked(identity, policy.capacity)
            self._evict_bytes_locked(policy.group, entry.estimated_bytes)
            if self._group_bytes[policy.group] + entry.estimated_bytes > group_limit:
                self._stats_for(identity).load_error += 1
                return False
            if self._total_bytes + entry.estimated_bytes > self._policy.total_bytes:
                self._stats_for(identity).load_error += 1
                return False
            self._entries[identity] = entry
            scope = (identity.dataset, identity.source)
            self._scope_entries[scope] = self._scope_entries.get(scope, 0) + 1
            self._scope_bytes[scope] = self._scope_bytes.get(scope, 0) + entry.estimated_bytes
            self._group_bytes[policy.group] += entry.estimated_bytes
            self._total_bytes += entry.estimated_bytes
            return True

    def _evict_scope_locked(self, identity: CacheIdentity, capacity: int) -> None:
        scope = (identity.dataset, identity.source)
        while self._scope_entries.get(scope, 0) >= capacity:
            scoped = [key for key in self._entries if key.dataset == identity.dataset and key.source == identity.source]
            if not scoped:
                raise RuntimeError("cache scope counters are inconsistent")
            victim = self._first_expired(scoped) or scoped[0]
            self._evict_locked(victim)

    def _evict_bytes_locked(self, group: str, incoming_bytes: int) -> None:
        group_limit = self._policy.groups[group].max_bytes
        while self._group_bytes[group] + incoming_bytes > group_limit:
            candidates = [key for key in self._entries if self._policy.datasets[key.dataset].group == group]
            if not candidates:
                break
            self._evict_locked(self._first_expired(candidates) or candidates[0])
        while self._total_bytes + incoming_bytes > self._policy.total_bytes and self._entries:
            candidates = list(self._entries)
            self._evict_locked(self._first_expired(candidates) or candidates[0])

    def _first_expired(self, identities: list[CacheIdentity]) -> CacheIdentity | None:
        now = self._monotonic()
        for identity in identities:
            entry = self._entries[identity]
            policy = self._policy.datasets[identity.dataset]
            if entry.value is None:
                negative_started = entry.negative_inserted_at or entry.inserted_at
                if now - negative_started > policy.negative_ttl_seconds:
                    return identity
                continue
            elapsed = max(0.0, now - entry.inserted_at)
            if self._value_state(identity, entry, elapsed) == "degraded":
                return identity
        return None

    def _evict_locked(self, identity: CacheIdentity) -> None:
        self._stats_for(identity).eviction += 1
        self._remove_locked(identity)

    def _remove_locked(self, identity: CacheIdentity) -> None:
        entry = self._entries.pop(identity, None)
        if entry is None:
            return
        group = self._policy.datasets[identity.dataset].group
        scope = (identity.dataset, identity.source)
        remaining_entries = self._scope_entries.get(scope, 0) - 1
        remaining_bytes = self._scope_bytes.get(scope, 0) - entry.estimated_bytes
        if remaining_entries > 0:
            self._scope_entries[scope] = remaining_entries
            self._scope_bytes[scope] = remaining_bytes
        else:
            self._scope_entries.pop(scope, None)
            self._scope_bytes.pop(scope, None)
        self._group_bytes[group] -= entry.estimated_bytes
        self._total_bytes -= entry.estimated_bytes

    def _timing(self, identity: CacheIdentity) -> tuple[float, float]:
        policy = self._dataset_policy(identity)
        if policy.cadence_task is None:
            return cast(float, policy.refresh_ttl_seconds), cast(float, policy.action_max_age_seconds)
        try:
            refresh = float(self._cadence_seconds[policy.cadence_task][identity.phase])
        except KeyError as exc:
            raise ValueError(
                f"missing cadence for cache dataset {identity.dataset}: {policy.cadence_task}.{identity.phase}"
            ) from exc
        return refresh, refresh * cast(float, policy.action_max_age_multiplier)

    def _business_age(self, source_time: datetime | None) -> float | None:
        if source_time is None:
            return None
        now = self._wall_clock()
        _require_aware(now, "cache wall clock")
        return max(0.0, (now - source_time).total_seconds())

    def _value_state(self, identity: CacheIdentity, entry: CacheEntry[_T], elapsed: float) -> str:
        policy = self._dataset_policy(identity)
        refresh_ttl, action_age = self._timing(identity)
        business_age = self._business_age(entry.source_time)
        if policy.cadence_task is not None:
            if business_age is not None and business_age > action_age:
                return "degraded"
            if business_age is not None and business_age > refresh_ttl * 2.0:
                return "stale"
            return "refresh_due" if elapsed > refresh_ttl else "fresh"
        if business_age is not None and business_age > action_age:
            return "degraded"
        return "refresh_due" if elapsed > refresh_ttl else "fresh"

    @staticmethod
    def _record_value_state(stats: _MutableStats, state: str) -> None:
        if state == "refresh_due":
            stats.refresh_due_hit += 1
        elif state == "stale":
            stats.stale_hit += 1
        elif state == "degraded":
            stats.degraded_hit += 1

    def _clear_negative_locked(
        self,
        identity: CacheIdentity,
        entry: CacheEntry[_T],
        now: float,
    ) -> CacheEntry[_T]:
        estimated_bytes = entry.value_estimated_bytes
        refresh_ttl, _action_age = self._timing(identity)
        refresh_due_inserted_at = math.nextafter(now - refresh_ttl, -math.inf)
        cleaned = CacheEntry(
            entry.value,
            entry.data_version,
            entry.source_time,
            None,
            min(entry.inserted_at, refresh_due_inserted_at),
            None,
            estimated_bytes,
            estimated_bytes,
        )
        group = self._policy.datasets[identity.dataset].group
        difference = estimated_bytes - entry.estimated_bytes
        scope = (identity.dataset, identity.source)
        self._scope_bytes[scope] = self._scope_bytes.get(scope, 0) + difference
        self._group_bytes[group] += difference
        self._total_bytes += difference
        self._entries[identity] = cleaned
        return cleaned

    @staticmethod
    def _estimate_entry(
        identity: CacheIdentity,
        value: object,
        data_version: str | None,
        source_time: datetime | None,
        error_code: str | None,
    ) -> int:
        payload = (
            {"error_code": error_code}
            if value is None and data_version is None and source_time is None and error_code is not None
            else {
                "data_version": data_version,
                "source_time": source_time,
                "value": value,
                "error_code": error_code,
            }
        )
        return len(canonical_json_bytes(identity.as_dict())) + len(canonical_json_bytes(payload))

    def _dataset_policy(self, identity: CacheIdentity) -> CacheDatasetPolicy:
        try:
            return self._policy.datasets[identity.dataset]
        except KeyError as exc:
            raise ValueError(f"unknown cache dataset: {identity.dataset}") from exc

    def _stats_for(self, identity: CacheIdentity) -> _MutableStats:
        return self._stats.setdefault((identity.dataset, identity.source), _MutableStats())


def _require_aware(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = ["BoundedLruCache", "CacheEntry"]
