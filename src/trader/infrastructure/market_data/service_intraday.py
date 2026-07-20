"""Intraday minute cache and bounded loading operations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from concurrent.futures import wait
from datetime import datetime
from typing import ParamSpec, TypeVar

from trader.application.workers import borrow_executor
from trader.domain.models import FeatureSnapshot
from trader.domain.tail import TAIL_SIGNAL_VALUE_FIELDS, MinuteBar
from trader.infrastructure.market_data.service_models import _IntradayEntry
from trader.infrastructure.market_data.service_state import MarketServiceState
from trader.infrastructure.market_data.service_support import _minute_version

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MarketIntradayMixin(MarketServiceState):
    def _load_intraday(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[MinuteBar, ...]]:
        now = self._monotonic()
        result: dict[str, tuple[MinuteBar, ...]] = {}
        previous: dict[str, _IntradayEntry] = {}
        with self._lock:
            self._intraday_requested_rows = len(codes)
            for code in codes:
                entry = self._intraday.get(code)
                if entry is not None:
                    previous[code] = entry
                if entry is not None and entry.expires_at > now:
                    result[code] = entry.bars
        missing = [code for code in codes if code not in result]
        if self._intraday_client is None:
            with self._lock:
                self._intraday_covered_rows = sum(bool(result.get(code)) for code in codes)
                self._intraday_last_error = "intraday_client_unavailable"
            return result
        if missing:
            with borrow_executor(
                self._worker_pool,
                worker_count=min(self._intraday_workers, len(missing)),
                thread_name_prefix="candidate-intraday",
                queue_capacity=len(missing),
                wait_on_exit=False,
            ) as pool:
                futures = {}
                for code in missing:
                    future = pool.submit(self._intraday_client.fetch_intraday_minutes, code, now=observed_at)
                    if future is None:
                        raise RuntimeError("data worker queue rejected intraday task")
                    futures[future] = code
                completed, pending = wait(futures, timeout=self._intraday_batch_timeout_seconds)
                for future in completed:
                    code = futures[future]
                    old_entry = previous.get(code)
                    ttl = self._intraday_ttl_seconds
                    used_fallback = False
                    try:
                        bars = tuple(future.result())
                    except (OSError, RuntimeError, ValueError) as exc:
                        bars = old_entry.bars if old_entry is not None else ()
                        used_fallback = old_entry is not None and bool(old_entry.bars)
                        ttl = min(15.0, ttl)
                        with self._lock:
                            self._intraday_error_count += 1
                            self._intraday_last_error = str(exc)[:240]
                    else:
                        with self._lock:
                            if bars:
                                self._intraday_success_count += 1
                            else:
                                self._intraday_error_count += 1
                                self._intraday_last_error = "empty_intraday_series"
                                if old_entry is not None and old_entry.bars:
                                    bars = old_entry.bars
                                    used_fallback = True
                    if bars and old_entry is not None and _minute_version(bars) < _minute_version(old_entry.bars):
                        bars = old_entry.bars
                        used_fallback = True
                        ttl = min(15.0, ttl)
                        with self._lock:
                            self._intraday_out_of_order_count += 1
                            self._intraday_last_error = "out_of_order_intraday_result"
                    result[code] = bars
                    if bars or old_entry is None:
                        with self._lock:
                            self._intraday[code] = _IntradayEntry(
                                bars,
                                self._monotonic() + (min(15.0, ttl) if used_fallback else ttl),
                            )
                for future in pending:
                    code = futures[future]
                    future.cancel()
                    old_entry = previous.get(code)
                    result[code] = old_entry.bars if old_entry is not None else ()
                    with self._lock:
                        self._intraday_error_count += 1
                        self._intraday_last_error = "intraday_batch_deadline"
                        if code not in previous:
                            self._intraday[code] = _IntradayEntry(
                                (),
                                self._monotonic() + min(15.0, self._intraday_ttl_seconds),
                            )
        with self._lock:
            self._intraday_covered_rows = sum(bool(result.get(code)) for code in codes)
            bars = tuple(bar for code in codes for bar in result.get(code, ()))
            self._intraday_latest_source_time = max(
                (bar.source_time.isoformat() for bar in bars),
                default="",
            )
            self._intraday_sources = tuple(sorted({bar.source for bar in bars if bar.source}))
            self._intraday_data_versions = tuple(sorted({bar.data_version for bar in bars if bar.data_version}))
            excess = len(self._intraday) - self._intraday_cache_limit
            if excess > 0:
                requested = set(codes)
                oldest = sorted(
                    self._intraday,
                    key=lambda code: (code in requested, self._intraday[code].expires_at, code),
                )[:excess]
                for code in oldest:
                    self._intraday.pop(code, None)
            if self._intraday_covered_rows == self._intraday_requested_rows:
                self._intraday_last_error = ""
        return result

    def _record_intraday_feature_coverage(
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
            self._intraday_requested_rows = len(codes)
            self._intraday_covered_rows = covered_rows
            if covered_rows == len(codes):
                self._intraday_last_error = ""
            elif not self._intraday_last_error:
                self._intraday_last_error = "intraday_series_incomplete"
