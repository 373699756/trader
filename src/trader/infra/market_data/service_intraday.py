"""Intraday minute cache and bounded loading operations."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import wait
from dataclasses import dataclass
from datetime import datetime
from typing import ParamSpec, TypeVar, cast

from trader.application.workers import borrow_executor, submit_or_run_inline
from trader.domain.market.models import FeatureSnapshot
from trader.domain.market.tail import TAIL_SIGNAL_VALUE_FIELDS, MinuteBar
from trader.infra.market_data.eastmoney import EastmoneyClient
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_models import _IntradayEntry
from trader.infra.market_data.service_support import (
    _add_action_restriction,
    _minute_version,
    _source_batch_identity,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(frozen=True)
class IntradayLoaderStatus:
    entries: int
    success_count: int
    error_count: int
    last_error: str
    out_of_order_count: int
    requested_rows: int
    covered_rows: int
    latest_source_time: str
    sources: tuple[str, ...]
    data_versions: tuple[str, ...]


class IntradayLoader:
    def __init__(
        self,
        client: EastmoneyClient | None,
        runner: MarketTaskRunner,
        *,
        workers: int,
        ttl_seconds: float,
        batch_timeout_seconds: float,
        capacity: int,
        monotonic: Callable[[], float],
    ) -> None:
        self._client = client
        self._runner = runner
        self._workers = max(1, workers)
        self._ttl_seconds = max(1.0, ttl_seconds)
        self._batch_timeout_seconds = max(0.01, batch_timeout_seconds)
        self._capacity = max(1, capacity)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, _IntradayEntry] = {}
        self._success_count = 0
        self._error_count = 0
        self._last_error = ""
        self._out_of_order_count = 0
        self._requested_rows = 0
        self._covered_rows = 0
        self._latest_source_time = ""
        self._sources: tuple[str, ...] = ()
        self._data_versions: tuple[str, ...] = ()

    def load(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, tuple[MinuteBar, ...]]:
        source_lanes = self._runner.source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread("eastmoney"):
            identity = _source_batch_identity("intraday_minutes", codes, observed_at)
            lane_action_restrictions: dict[str, set[str]] = {}
            lane_future = source_lanes.submit(
                "eastmoney",
                identity,
                observed_at,
                self.load,
                codes,
                observed_at,
                action_restrictions=lane_action_restrictions,
            )
            try:
                lane_result = lane_future.result(timeout=self._batch_timeout_seconds)
            except FutureTimeoutError:
                lane_future.cancel()
                with self._lock:
                    now = self._monotonic()
                    fallback = {
                        code: entry.bars
                        for code in codes
                        if (entry := self._entries.get(code)) is not None and entry.expires_at > now
                    }
                    self._requested_rows = len(codes)
                    self._covered_rows = sum(bool(fallback.get(code)) for code in codes)
                    self._error_count += 1
                    self._last_error = "intraday_batch_deadline"
                cache = self._runner.cache
                if cache is not None:
                    for code, bars in fallback.items():
                        if not bars:
                            continue
                        cache_identity = self._runner.cache_identity(
                            "intraday_minutes",
                            "eastmoney",
                            code,
                            {"code": code, "scale_minutes": 1, "adjust": "none"},
                            observed_at,
                        )
                        if not cache.is_actionable(cache_identity, max(bar.source_time for bar in bars)):
                            _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
                return fallback
            for code, restrictions in lane_action_restrictions.items():
                for restriction in restrictions:
                    _add_action_restriction(action_restrictions, code, restriction)
            return lane_result
        now = self._monotonic()
        result: dict[str, tuple[MinuteBar, ...]] = {}
        previous: dict[str, _IntradayEntry] = {}
        cache = self._runner.cache
        if cache is not None:
            for code in codes:
                cache_identity = self._runner.cache_identity(
                    "intraday_minutes",
                    "eastmoney",
                    code,
                    {"code": code, "scale_minutes": 1, "adjust": "none"},
                    observed_at,
                )
                lookup = cache.get(cache_identity)
                if (
                    lookup is not None
                    and lookup.value is not None
                    and (lookup.state == "fresh" or lookup.retry_suppressed)
                ):
                    result[code] = cast(tuple[MinuteBar, ...], lookup.value)
                    if lookup.state == "degraded":
                        _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
        with self._lock:
            self._requested_rows = len(codes)
            for code in codes:
                entry = self._entries.get(code)
                if entry is not None:
                    previous[code] = entry
                if code not in result and entry is not None and entry.expires_at > now:
                    result[code] = entry.bars
                    if cache is not None and entry.bars:
                        cache_identity = self._runner.cache_identity(
                            "intraday_minutes",
                            "eastmoney",
                            code,
                            {"code": code, "scale_minutes": 1, "adjust": "none"},
                            observed_at,
                        )
                        if not cache.is_actionable(
                            cache_identity,
                            max(bar.source_time for bar in entry.bars),
                        ):
                            _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
        missing = [code for code in codes if code not in result]
        if self._client is None:
            with self._lock:
                self._covered_rows = sum(bool(result.get(code)) for code in codes)
                self._last_error = "intraday_client_unavailable"
            return result
        if missing:
            batch_deadline = self._monotonic() + self._batch_timeout_seconds
            nested_inline = source_lanes is not None and source_lanes.owns_current_thread("eastmoney")
            with borrow_executor(
                self._runner.worker_pool,
                worker_count=min(self._workers, len(missing)),
                thread_name_prefix="candidate-intraday",
                queue_capacity=len(missing),
                wait_on_exit=False,
                nested_inline=nested_inline,
            ) as pool:
                futures = {}
                timed_out_codes: list[str] = []
                for index, code in enumerate(missing):
                    if nested_inline and self._monotonic() >= batch_deadline:
                        timed_out_codes.extend(missing[index:])
                        break
                    future = submit_or_run_inline(
                        pool,
                        self._client.fetch_intraday_minutes,
                        code,
                        now=observed_at,
                    )
                    if nested_inline and self._monotonic() >= batch_deadline:
                        timed_out_codes.extend(missing[index:])
                        break
                    futures[future] = code
                completed, pending = wait(
                    futures,
                    timeout=max(0.0, batch_deadline - self._monotonic()),
                )
                for future in completed:
                    code = futures[future]
                    old_entry = previous.get(code)
                    ttl = self._ttl_seconds
                    used_fallback = False
                    try:
                        bars = tuple(future.result())
                    except (OSError, RuntimeError, ValueError) as exc:
                        bars = old_entry.bars if old_entry is not None else ()
                        used_fallback = old_entry is not None and bool(old_entry.bars)
                        ttl = min(15.0, ttl)
                        with self._lock:
                            self._error_count += 1
                            self._last_error = str(exc)[:240]
                    else:
                        with self._lock:
                            if bars:
                                self._success_count += 1
                            else:
                                self._error_count += 1
                                self._last_error = "empty_intraday_series"
                                if old_entry is not None and old_entry.bars:
                                    bars = old_entry.bars
                                    used_fallback = True
                    if bars and old_entry is not None and _minute_version(bars) < _minute_version(old_entry.bars):
                        bars = old_entry.bars
                        used_fallback = True
                        ttl = min(15.0, ttl)
                        with self._lock:
                            self._out_of_order_count += 1
                            self._last_error = "out_of_order_intraday_result"
                    result[code] = bars
                    if cache is not None:
                        cache_identity = self._runner.cache_identity(
                            "intraday_minutes",
                            "eastmoney",
                            code,
                            {"code": code, "scale_minutes": 1, "adjust": "none"},
                            observed_at,
                        )
                        if bars:
                            cache.put(
                                cache_identity,
                                bars,
                                data_version=max(bar.data_version for bar in bars),
                                source_time=max(bar.source_time for bar in bars),
                            )
                            if used_fallback:
                                cache.put_negative(cache_identity, error_code="intraday_refresh_failed")
                            if not cache.is_actionable(cache_identity, max(bar.source_time for bar in bars)):
                                _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
                        else:
                            cache.put_negative(cache_identity, error_code="intraday_no_data")
                    if bars or old_entry is None:
                        with self._lock:
                            self._entries[code] = _IntradayEntry(
                                bars,
                                self._monotonic() + (min(15.0, ttl) if used_fallback else ttl),
                            )
                for future in pending:
                    future.cancel()
                    timed_out_codes.append(futures[future])
                for code in dict.fromkeys(timed_out_codes):
                    old_entry = previous.get(code)
                    result[code] = old_entry.bars if old_entry is not None else ()
                    if cache is not None:
                        cache_identity = self._runner.cache_identity(
                            "intraday_minutes",
                            "eastmoney",
                            code,
                            {"code": code, "scale_minutes": 1, "adjust": "none"},
                            observed_at,
                        )
                        if old_entry is not None and old_entry.bars:
                            cache.put(
                                cache_identity,
                                old_entry.bars,
                                data_version=max(bar.data_version for bar in old_entry.bars),
                                source_time=max(bar.source_time for bar in old_entry.bars),
                            )
                        cache.put_negative(cache_identity, error_code="intraday_batch_deadline")
                        if (
                            old_entry is not None
                            and old_entry.bars
                            and not cache.is_actionable(
                                cache_identity,
                                max(bar.source_time for bar in old_entry.bars),
                            )
                        ):
                            _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
                    with self._lock:
                        self._error_count += 1
                        self._last_error = "intraday_batch_deadline"
                        if code not in previous:
                            self._entries[code] = _IntradayEntry(
                                (),
                                self._monotonic() + min(15.0, self._ttl_seconds),
                            )
        with self._lock:
            self._covered_rows = sum(bool(result.get(code)) for code in codes)
            bars = tuple(bar for code in codes for bar in result.get(code, ()))
            self._latest_source_time = max(
                (bar.source_time.isoformat() for bar in bars),
                default="",
            )
            self._sources = tuple(sorted({bar.source for bar in bars if bar.source}))
            self._data_versions = tuple(sorted({bar.data_version for bar in bars if bar.data_version}))
            excess = len(self._entries) - self._capacity
            if excess > 0:
                requested = set(codes)
                oldest = sorted(
                    self._entries,
                    key=lambda code: (code in requested, self._entries[code].expires_at, code),
                )[:excess]
                for code in oldest:
                    self._entries.pop(code, None)
            if self._covered_rows == self._requested_rows:
                self._last_error = ""
        return result

    def record_feature_coverage(
        self,
        codes: Sequence[str],
        features: Sequence[FeatureSnapshot],
    ) -> None:
        covered_codes = {
            feature.quote.code
            for feature in features
            if all(feature.optional_value(field) is not None for field in TAIL_SIGNAL_VALUE_FIELDS)
        }
        covered_rows = sum(code in covered_codes for code in codes)
        with self._lock:
            self._requested_rows = len(codes)
            self._covered_rows = covered_rows
            if covered_rows == len(codes):
                self._last_error = ""
            elif not self._last_error:
                self._last_error = "intraday_series_incomplete"

    def cached(
        self,
        codes: Sequence[str],
        *,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, tuple[MinuteBar, ...]]:
        now = self._monotonic()
        result: dict[str, tuple[MinuteBar, ...]] = {}
        cache = self._runner.cache
        if cache is not None:
            observed_at = self._runner.wall_clock()
            for code in codes:
                identity = self._runner.cache_identity(
                    "intraday_minutes",
                    "eastmoney",
                    code,
                    {"code": code, "scale_minutes": 1, "adjust": "none"},
                    observed_at,
                )
                lookup = cache.get(identity)
                if lookup is not None and lookup.value is not None:
                    result[code] = cast(tuple[MinuteBar, ...], lookup.value)
                    if lookup.state == "degraded":
                        _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
        with self._lock:
            for code in codes:
                if code in result:
                    continue
                entry = self._entries.get(code)
                if entry is None or entry.expires_at <= now:
                    continue
                result[code] = entry.bars
                if cache is not None and entry.bars:
                    identity = self._runner.cache_identity(
                        "intraday_minutes",
                        "eastmoney",
                        code,
                        {"code": code, "scale_minutes": 1, "adjust": "none"},
                        observed_at,
                    )
                    if not cache.is_actionable(identity, max(bar.source_time for bar in entry.bars)):
                        _add_action_restriction(action_restrictions, code, "intraday_data_degraded")
        return result

    def status(self) -> IntradayLoaderStatus:
        with self._lock:
            return IntradayLoaderStatus(
                entries=len(self._entries),
                success_count=self._success_count,
                error_count=self._error_count,
                last_error=self._last_error,
                out_of_order_count=self._out_of_order_count,
                requested_rows=self._requested_rows,
                covered_rows=self._covered_rows,
                latest_source_time=self._latest_source_time,
                sources=self._sources,
                data_versions=self._data_versions,
            )

    def entries(self) -> Mapping[str, _IntradayEntry]:
        with self._lock:
            return dict(self._entries)


__all__ = ["IntradayLoader", "IntradayLoaderStatus"]
