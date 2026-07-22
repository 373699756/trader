"""Daily history cache and bounded loading operations."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import as_completed, wait
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import ParamSpec, TypeVar, cast
from zoneinfo import ZoneInfo

from trader.application.cache import build_cache_identity, request_fingerprint
from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.workers import BoundedExecutor, borrow_executor, submit_or_run_inline
from trader.domain.outcome.models import OutcomeBar
from trader.infra.market_data.history import DailyBar, HistoryProfile, summarize_history_metrics
from trader.infra.market_data.history_seed import DailyHistoryClient
from trader.infra.market_data.service_execution import MarketTaskRunner
from trader.infra.market_data.service_models import _HistoryEntry
from trader.infra.market_data.service_support import (
    _add_action_restriction,
    _history_version,
    _source_batch_identity,
)

_P = ParamSpec("_P")
_T = TypeVar("_T")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HISTORY_SOURCE_LANE = "history"


@dataclass(frozen=True)
class HistoryStoreStatus:
    entries: int
    universe_rows: int
    covered_rows: int
    error_count: int
    data_versions: tuple[str, ...]
    out_of_order_count: int


class HistoryStore:
    def __init__(
        self,
        history_client: DailyHistoryClient,
        runner: MarketTaskRunner,
        *,
        history_worker_pool: BoundedExecutor | None,
        workers: int,
        ttl_seconds: float,
        capacity: int,
        monotonic: Callable[[], float],
    ) -> None:
        self._history_client = history_client
        self._runner = runner
        self._history_worker_pool = history_worker_pool
        self._history_workers = max(1, workers)
        self._history_ttl_seconds = max(60.0, ttl_seconds)
        self._history_cache_limit = max(1, capacity)
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._history: dict[str, _HistoryEntry] = {}
        self._history_error_count = 0
        self._history_out_of_order_count = 0
        self._history_universe_rows = 0
        self._history_covered_rows = 0
        self._history_data_versions: tuple[str, ...] = ()

    def load(
        self,
        codes: Sequence[str],
        *,
        force: bool = False,
        deadline: datetime | None = None,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        self._runner.ensure_before_deadline(deadline)
        source_lanes = self._runner.source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread(_HISTORY_SOURCE_LANE):
            observed_at = self._runner.wall_clock()
            identity = _source_batch_identity(
                "daily_history",
                codes,
                observed_at,
                force=force,
                deadline=deadline,
            )
            lane_future = source_lanes.submit(
                _HISTORY_SOURCE_LANE,
                identity,
                observed_at,
                self.load,
                codes,
                force=force,
                deadline=deadline,
                action_restrictions=action_restrictions,
            )
            if deadline is None:
                return lane_future.result()
            remaining = max(0.0, (deadline - self._runner.wall_clock()).total_seconds())
            try:
                lane_result = lane_future.result(timeout=remaining)
            except FutureTimeoutError as exc:
                lane_future.cancel()
                with self._lock:
                    self._history_error_count += 1
                raise MarketDataDeadlineExceededError("history source lane exceeded its batch deadline") from exc
            self._runner.ensure_before_deadline(deadline)
            return lane_result
        result = (
            {}
            if force
            else self.cached(
                codes,
                fresh_only=True,
                action_restrictions=action_restrictions,
            )
        )
        cache_observed_at = self._runner.wall_clock() if self._runner.cache is not None else None
        with self._lock:
            previous = {code: self._history[code] for code in codes if code in self._history}
        missing = [code for code in codes if force or code not in result]
        if not missing:
            self._runner.ensure_before_deadline(deadline)
            return result
        history_pool = self._history_worker_pool or self._runner.worker_pool
        with borrow_executor(
            history_pool,
            worker_count=min(self._history_workers, len(missing)),
            thread_name_prefix="candidate-history",
            queue_capacity=len(missing),
            wait_on_exit=deadline is None,
            nested_inline=(
                history_pool is self._runner.worker_pool
                and source_lanes is not None
                and source_lanes.owns_current_thread(_HISTORY_SOURCE_LANE)
            ),
        ) as pool:
            futures = {}
            for code in missing:
                self._runner.ensure_before_deadline(deadline)
                future = submit_or_run_inline(pool, self._history_client.fetch_history, code, days=90)
                self._runner.ensure_before_deadline(deadline)
                futures[future] = code
            if deadline is None:
                completed = as_completed(futures)
            else:
                timeout = max(0.0, (deadline - self._runner.wall_clock()).total_seconds())
                completed_set, pending = wait(futures, timeout=timeout)
                if pending:
                    for future in pending:
                        future.cancel()
                    with self._lock:
                        self._history_error_count += len(pending)
                    raise MarketDataDeadlineExceededError("history preload exceeded its batch deadline")
                completed = iter(completed_set)
            self._runner.ensure_before_deadline(deadline)
            pending_entries: dict[str, _HistoryEntry] = {}
            for future in completed:
                self._runner.ensure_before_deadline(deadline)
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
                self._runner.ensure_before_deadline(deadline)
                if bars and old_entry is not None and _history_version(bars) < _history_version(old_entry.bars):
                    bars = old_entry.bars
                    used_fallback = True
                    with self._lock:
                        self._history_out_of_order_count += 1
                elif not bars and old_entry is not None and old_entry.bars:
                    bars = old_entry.bars
                    used_fallback = True
                result[code] = bars
                cache = self._runner.cache
                if cache is not None:
                    self._runner.ensure_before_deadline(deadline)
                    assert cache_observed_at is not None
                    cache_identity = self._runner.cache_identity(
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
                    source=old_entry.source if used_fallback and old_entry is not None else "eastmoney",
                )
            self._runner.ensure_before_deadline(deadline)
            with self._lock:
                self._runner.ensure_before_deadline(deadline)
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
                self.trim(set(codes))
        if cache is not None:
            assert cache_observed_at is not None
            with self._lock:
                history_sources = {
                    code: entry.source for code in result if (entry := self._history.get(code)) is not None
                }
            for code, bars in result.items():
                if not bars:
                    continue
                if history_sources.get(code) == "tushare":
                    continue
                cache_identity = self._runner.cache_identity(
                    "daily_history",
                    "eastmoney",
                    code,
                    {"code": code, "days": 90, "adjust": "qfq"},
                    cache_observed_at,
                )
                if not cache.is_actionable(cache_identity, _history_source_time(bars)):
                    _add_action_restriction(action_restrictions, code, "history_data_degraded")
        return result

    def cached(
        self,
        codes: Iterable[str],
        *,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(codes)
        now = self._monotonic()
        result: dict[str, tuple[DailyBar, ...]] = {}
        cache = self._runner.cache
        if cache is not None:
            observed_at = self._runner.wall_clock()
            for code in requested:
                identity = self._runner.cache_identity(
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
            history_sources: dict[str, str] = {}
            for code in requested:
                entry = self._history.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                history_sources[code] = entry.source
                cached = result.get(code)
                if cached is None or _history_version(entry.bars) > _history_version(cached):
                    result[code] = entry.bars
        if cache is not None:
            for code, bars in result.items():
                if not bars:
                    continue
                if history_sources.get(code) == "tushare":
                    continue
                identity = self._runner.cache_identity(
                    "daily_history",
                    "eastmoney",
                    code,
                    {"code": code, "days": 90, "adjust": "qfq"},
                    observed_at,
                )
                if not cache.is_actionable(identity, _history_source_time(bars)):
                    _add_action_restriction(action_restrictions, code, "history_data_degraded")
        return result

    def trim(self, requested: set[str]) -> None:
        excess = len(self._history) - self._history_cache_limit
        if excess <= 0:
            return
        victims = sorted(
            self._history,
            key=lambda code: (code in requested, self._history[code].expires_at, code),
        )[:excess]
        for code in victims:
            self._history.pop(code, None)

    def update_coverage(self, codes: Sequence[str], data_versions: Sequence[str] | None = None) -> None:
        now = self._monotonic()
        with self._lock:
            self._history_universe_rows = len(codes)
            self._history_covered_rows = sum(
                (entry := self._history.get(code)) is not None and entry.expires_at > now and len(entry.bars) >= 20
                for code in codes
            )
            if data_versions is not None:
                self._history_data_versions = tuple(sorted(set(data_versions)))

    def apply_source_bars(
        self,
        bars_by_code: Mapping[str, Sequence[DailyBar]],
        *,
        source: str,
    ) -> None:
        expires_at = self._monotonic() + self._history_ttl_seconds
        with self._lock:
            for code, bars in bars_by_code.items():
                ordered = tuple(sorted(bars, key=lambda item: item.trade_date))[-90:]
                if not ordered:
                    continue
                current = self._history.get(code)
                if current is None or not current.bars or ordered[-1].trade_date > current.bars[-1].trade_date:
                    self._history[code] = _HistoryEntry(ordered, expires_at, source=source)
            self.trim(set(bars_by_code))

    def summaries(
        self,
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
    ) -> Mapping[str, HistoryProfile]:
        summaries: dict[str, HistoryProfile] = {}
        for code, bars in histories.items():
            material = tuple(
                (
                    bar.trade_date,
                    _finite_or_none(bar.open_price),
                    _finite_or_none(bar.close),
                    _finite_or_none(bar.high),
                    _finite_or_none(bar.low),
                    _finite_or_none(bar.volume),
                    _finite_or_none(bar.amount),
                    _finite_or_none(bar.pct_change),
                    _finite_or_none(bar.turnover_rate),
                )
                for bar in bars
            )
            history_version = request_fingerprint({"bars": material})[:24]
            identity = build_cache_identity(
                dataset="history_summary",
                source="history-summary",
                subject_key=code,
                request={"history_version": history_version},
                trade_date="versioned",
                phase="all_day",
                source_contract_version="history-summary-v16",
                config_version=self._runner.config_version,
                schema_version=self._runner.schema_version,
            )
            cache = self._runner.cache
            cached = cache.get(identity) if cache is not None else None
            if (
                cached is not None
                and isinstance(cached.value, HistoryProfile)
                and cached.state not in {"negative", "degraded"}
            ):
                summaries[code] = cached.value
                continue
            if cache is None:
                summaries[code] = summarize_history_metrics(bars)
                continue

            def load_summary(bars: tuple[DailyBar, ...] = bars) -> HistoryProfile:
                return summarize_history_metrics(bars)

            summary = cache.coalesce(identity, load_summary)
            if not isinstance(summary, HistoryProfile):
                raise TypeError("history summary cache returned an invalid value")
            cache.put(
                identity,
                summary,
                data_version=f"history:{history_version}",
                source_time=observed_at,
            )
            summaries[code] = summary
        return summaries

    def status(self) -> HistoryStoreStatus:
        with self._lock:
            return HistoryStoreStatus(
                entries=len(self._history),
                universe_rows=self._history_universe_rows,
                covered_rows=self._history_covered_rows,
                error_count=self._history_error_count,
                data_versions=self._history_data_versions,
                out_of_order_count=self._history_out_of_order_count,
            )

    def entries(self) -> Mapping[str, _HistoryEntry]:
        with self._lock:
            return dict(self._history)

    def available_seed_codes(self, codes: Sequence[str]) -> tuple[str, ...]:
        available_codes = getattr(self._history_client, "available_codes", None)
        if not callable(available_codes):
            return ()
        return tuple(available_codes(codes))

    def read_outcome_bars(
        self,
        codes: Sequence[str],
        observed_at: datetime,
    ) -> Mapping[str, tuple[OutcomeBar, ...]]:
        del observed_at
        histories = self.load(codes, force=True)
        return {
            code: tuple(
                OutcomeBar(
                    trade_date=bar.trade_date,
                    open_price=bar.open_price,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    pct_change=bar.pct_change,
                )
                for bar in bars
            )
            for code, bars in histories.items()
        }


def _history_source_time(bars: Sequence[DailyBar]) -> datetime:
    latest = date.fromisoformat(_history_version(bars))
    return datetime.combine(latest, time(15, 0), _SHANGHAI)


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None
