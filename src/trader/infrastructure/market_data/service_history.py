"""Daily history cache and bounded loading operations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import as_completed, wait
from datetime import date, datetime, time
from typing import ParamSpec, TypeVar, cast
from zoneinfo import ZoneInfo

from trader.application.ports import MarketDataDeadlineExceeded
from trader.application.workers import borrow_executor, submit_or_run_inline
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.service_models import _HistoryEntry
from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import (
    _add_action_restriction,
    _history_version,
    _source_batch_identity,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class MarketHistoryMixin(MarketServiceState):
    def _load_histories(
        self,
        codes: Sequence[str],
        *,
        force: bool = False,
        deadline: datetime | None = None,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        self._ensure_before_deadline(deadline)
        source_lanes = self._source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread("eastmoney"):
            observed_at = self._wall_clock()
            identity = _source_batch_identity(
                "daily_history",
                codes,
                observed_at,
                force=force,
                deadline=deadline,
            )
            lane_future = source_lanes.submit(
                "eastmoney",
                identity,
                observed_at,
                self._load_histories,
                codes,
                force=force,
                deadline=deadline,
                action_restrictions=action_restrictions,
            )
            if deadline is None:
                return lane_future.result()
            remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
            try:
                lane_result = lane_future.result(timeout=remaining)
            except FutureTimeoutError as exc:
                lane_future.cancel()
                with self._lock:
                    self._history_error_count += 1
                raise MarketDataDeadlineExceeded("history source lane exceeded its batch deadline") from exc
            self._ensure_before_deadline(deadline)
            return lane_result
        result = (
            {}
            if force
            else self._cached_histories(
                codes,
                fresh_only=True,
                action_restrictions=action_restrictions,
            )
        )
        cache_observed_at = self._wall_clock() if self._cache is not None else None
        with self._lock:
            previous = {code: self._history[code] for code in codes if code in self._history}
        missing = [code for code in codes if force or code not in result]
        if not missing:
            self._ensure_before_deadline(deadline)
            return result
        with borrow_executor(
            self._worker_pool,
            worker_count=min(self._history_workers, len(missing)),
            thread_name_prefix="candidate-history",
            queue_capacity=len(missing),
            wait_on_exit=deadline is None,
            nested_inline=source_lanes is not None and source_lanes.owns_current_thread("eastmoney"),
        ) as pool:
            futures = {}
            for code in missing:
                self._ensure_before_deadline(deadline)
                future = submit_or_run_inline(pool, self._history_client.fetch_history, code, days=90)
                self._ensure_before_deadline(deadline)
                futures[future] = code
            if deadline is None:
                completed = as_completed(futures)
            else:
                timeout = max(0.0, (deadline - self._wall_clock()).total_seconds())
                completed_set, pending = wait(futures, timeout=timeout)
                if pending:
                    for future in pending:
                        future.cancel()
                    with self._lock:
                        self._history_error_count += len(pending)
                    raise MarketDataDeadlineExceeded("history preload exceeded its batch deadline")
                completed = iter(completed_set)
            self._ensure_before_deadline(deadline)
            pending_entries: dict[str, _HistoryEntry] = {}
            for future in completed:
                self._ensure_before_deadline(deadline)
                code = futures[future]
                with self._lock:
                    old_entry = self._history.get(code) or previous.get(code)
                used_fallback = False
                try:
                    bars = tuple(future.result())
                except Exception:
                    bars = ()
                    with self._lock:
                        self._history_error_count += 1
                self._ensure_before_deadline(deadline)
                if bars and old_entry is not None and _history_version(bars) < _history_version(old_entry.bars):
                    bars = old_entry.bars
                    used_fallback = True
                    with self._lock:
                        self._history_out_of_order_count += 1
                elif not bars and old_entry is not None and old_entry.bars:
                    bars = old_entry.bars
                    used_fallback = True
                result[code] = bars
                cache = self._cache
                if cache is not None:
                    self._ensure_before_deadline(deadline)
                    assert cache_observed_at is not None
                    cache_identity = self._data_cache_identity(
                        "daily_history",
                        "eastmoney",
                        code,
                        {"code": code, "days": 90, "adjust": "qfq"},
                        cache_observed_at,
                    )
                    if bars:
                        cache.put(
                            cache_identity,
                            bars,
                            data_version=_history_version(bars),
                            source_time=_history_source_time(bars),
                        )
                        if used_fallback:
                            cache.put_negative(cache_identity, error_code="history_refresh_failed")
                    else:
                        cache.put_negative(cache_identity, error_code="history_no_data")
                pending_entries[code] = _HistoryEntry(
                    bars=bars,
                    expires_at=self._monotonic()
                    + (
                        min(60.0, self._history_ttl_seconds) if used_fallback or not bars else self._history_ttl_seconds
                    ),
                )
            self._ensure_before_deadline(deadline)
            with self._lock:
                self._ensure_before_deadline(deadline)
                for code, incoming in tuple(pending_entries.items()):
                    current = self._history.get(code)
                    if (
                        current is not None
                        and current.bars
                        and (not incoming.bars or _history_version(incoming.bars) < _history_version(current.bars))
                    ):
                        if incoming.bars:
                            self._history_out_of_order_count += 1
                        pending_entries[code] = current
                        result[code] = current.bars
                self._history.update(pending_entries)
                self._trim_history_fallback_locked(set(codes))
        if cache is not None:
            assert cache_observed_at is not None
            for code, bars in result.items():
                if not bars:
                    continue
                cache_identity = self._data_cache_identity(
                    "daily_history",
                    "eastmoney",
                    code,
                    {"code": code, "days": 90, "adjust": "qfq"},
                    cache_observed_at,
                )
                if not cache.is_actionable(cache_identity, _history_source_time(bars)):
                    _add_action_restriction(action_restrictions, code, "history_data_degraded")
        return result

    def _cached_histories(
        self,
        codes: Iterable[str],
        *,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(codes)
        now = self._monotonic()
        result: dict[str, tuple[DailyBar, ...]] = {}
        cache = self._cache
        if cache is not None:
            observed_at = self._wall_clock()
            for code in requested:
                identity = self._data_cache_identity(
                    "daily_history",
                    "eastmoney",
                    code,
                    {"code": code, "days": 90, "adjust": "qfq"},
                    observed_at,
                )
                lookup = cache.get(identity)
                if (
                    lookup is not None
                    and lookup.value is not None
                    and (not fresh_only or lookup.state == "fresh" or lookup.retry_suppressed)
                ):
                    result[code] = cast(tuple[DailyBar, ...], lookup.value)
        with self._lock:
            for code in requested:
                entry = self._history.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                cached = result.get(code)
                if cached is None or _history_version(entry.bars) > _history_version(cached):
                    result[code] = entry.bars
        if cache is not None:
            for code, bars in result.items():
                if not bars:
                    continue
                identity = self._data_cache_identity(
                    "daily_history",
                    "eastmoney",
                    code,
                    {"code": code, "days": 90, "adjust": "qfq"},
                    observed_at,
                )
                if not cache.is_actionable(identity, _history_source_time(bars)):
                    _add_action_restriction(action_restrictions, code, "history_data_degraded")
        return result


def _history_source_time(bars: Sequence[DailyBar]) -> datetime:
    latest = date.fromisoformat(_history_version(bars))
    return datetime.combine(latest, time(15, 0), _SHANGHAI)
