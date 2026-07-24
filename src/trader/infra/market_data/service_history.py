"""Daily history cache and bounded loading operations."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, as_completed, wait
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, ParamSpec, TypedDict, TypeVar, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.cache import CacheIdentity
from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.workers import BorrowExecutorOptions, BoundedExecutor, borrow_executor, submit_or_run_inline
from trader.domain.outcome.models import OutcomeBar
from trader.infra.market_data.history import (
    DailyBar,
    HistoryContext,
    PriceAdjustment,
    build_history_context,
    require_qfq_history,
)
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
    raw_rows: int
    profile_entries: int
    universe_rows: int
    covered_rows: int
    error_count: int
    data_versions: tuple[str, ...]
    out_of_order_count: int


@dataclass(frozen=True)
class _HistoryLoadRequest:
    codes: tuple[str, ...]
    force: bool
    deadline: datetime | None
    action_restrictions: dict[str, set[str]] | None


@dataclass
class _HistoryLoadState:
    request: _HistoryLoadRequest
    result: dict[str, tuple[DailyBar, ...]]
    previous: dict[str, _HistoryEntry]
    cache_observed_at: datetime | None
    pending_entries: dict[str, _HistoryEntry]


class HistoryStoreOptions(TypedDict):
    history_worker_pool: BoundedExecutor | None
    workers: int
    ttl_seconds: float
    capacity: int
    monotonic: Callable[[], float]


class HistoryStore:
    def __init__(
        self,
        history_client: DailyHistoryClient,
        runner: MarketTaskRunner,
        **options: Unpack[HistoryStoreOptions],
    ) -> None:
        self._history_client = history_client
        self._runner = runner
        self._history_worker_pool = options["history_worker_pool"]
        self._history_workers = max(1, options["workers"])
        self._history_ttl_seconds = max(60.0, options["ttl_seconds"])
        self._history_cache_limit = max(1, options["capacity"])
        self._monotonic = options["monotonic"]
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
        request = _HistoryLoadRequest(tuple(codes), force, deadline, action_restrictions)
        self._runner.ensure_before_deadline(request.deadline)
        source_lanes = self._runner.source_lanes
        if source_lanes is not None and not source_lanes.owns_current_thread(_HISTORY_SOURCE_LANE):
            return self._load_via_source_lane(request)
        return self._load_local(request)

    def _load_via_source_lane(
        self,
        request: _HistoryLoadRequest,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        source_lanes = self._runner.source_lanes
        assert source_lanes is not None
        observed_at = self._runner.wall_clock()
        identity = _source_batch_identity(
            "daily_history",
            request.codes,
            observed_at,
            force=request.force,
            deadline=request.deadline,
        )
        lane_future = source_lanes.submit(
            _HISTORY_SOURCE_LANE,
            identity,
            observed_at,
            self.load,
            request.codes,
            force=request.force,
            deadline=request.deadline,
            action_restrictions=request.action_restrictions,
        )
        if request.deadline is None:
            return lane_future.result()
        remaining = max(0.0, (request.deadline - self._runner.wall_clock()).total_seconds())
        try:
            lane_result = lane_future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            lane_future.cancel()
            with self._lock:
                self._history_error_count += 1
            raise MarketDataDeadlineExceededError("history source lane exceeded its batch deadline") from exc
        self._runner.ensure_before_deadline(request.deadline)
        return lane_result

    def _load_local(
        self,
        request: _HistoryLoadRequest,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        result = (
            {}
            if request.force
            else self.cached(
                request.codes,
                fresh_only=True,
                action_restrictions=request.action_restrictions,
            )
        )
        cache_observed_at = self._runner.wall_clock() if self._runner.cache is not None else None
        with self._lock:
            previous = {code: self._history[code] for code in request.codes if code in self._history}
        missing = [code for code in request.codes if request.force or code not in result]
        if not missing:
            self._runner.ensure_before_deadline(request.deadline)
            return result
        state = _HistoryLoadState(request, result, previous, cache_observed_at, {})
        self._fetch_missing_history(state, missing)
        self._mark_non_actionable_history(state)
        return result

    def _fetch_missing_history(
        self,
        state: _HistoryLoadState,
        missing: Sequence[str],
    ) -> None:
        request = state.request
        source_lanes = self._runner.source_lanes
        history_pool = self._history_worker_pool or self._runner.worker_pool
        with borrow_executor(
            history_pool,
            BorrowExecutorOptions(
                worker_count=min(self._history_workers, len(missing)),
                thread_name_prefix="candidate-history",
                queue_capacity=len(missing),
                wait_on_exit=request.deadline is None,
                nested_inline=(
                    history_pool is self._runner.worker_pool
                    and source_lanes is not None
                    and source_lanes.owns_current_thread(_HISTORY_SOURCE_LANE)
                ),
            ),
        ) as pool:
            futures = {}
            for code in missing:
                self._runner.ensure_before_deadline(request.deadline)
                future = submit_or_run_inline(pool, self._history_client.fetch_history, code, days=61)
                self._runner.ensure_before_deadline(request.deadline)
                futures[future] = code
            completed = self._completed_history_futures(futures, request.deadline)
            for future in completed:
                self._runner.ensure_before_deadline(request.deadline)
                code = futures[future]
                self._consume_history_future(state, code, future)
        self._commit_history_entries(state)

    def _completed_history_futures(
        self,
        futures: Mapping[Future[Sequence[DailyBar]], str],
        deadline: datetime | None,
    ) -> Iterable[Future[Sequence[DailyBar]]]:
        if deadline is None:
            return as_completed(futures)
        timeout = max(0.0, (deadline - self._runner.wall_clock()).total_seconds())
        completed, pending = wait(futures, timeout=timeout)
        if not pending:
            return completed
        for future in pending:
            future.cancel()
        with self._lock:
            self._history_error_count += len(pending)
        raise MarketDataDeadlineExceededError("history preload exceeded its batch deadline")

    def _consume_history_future(
        self,
        state: _HistoryLoadState,
        code: str,
        future: Future[Sequence[DailyBar]],
    ) -> None:
        with self._lock:
            old_entry = self._history.get(code) or state.previous.get(code)
        used_fallback = False
        try:
            bars = tuple(sorted(future.result(), key=lambda item: item.trade_date))[-61:]
        except Exception:
            bars = ()
            with self._lock:
                self._history_error_count += 1
        if any(bar.adjustment is not PriceAdjustment.QFQ for bar in bars):
            bars = ()
            with self._lock:
                self._history_error_count += 1
        self._runner.ensure_before_deadline(state.request.deadline)
        if bars and old_entry is not None and _history_version(bars) < _history_version(old_entry.bars):
            bars = old_entry.bars
            used_fallback = True
            with self._lock:
                self._history_out_of_order_count += 1
        elif not bars and old_entry is not None and old_entry.bars:
            bars = old_entry.bars
            used_fallback = True
        context = old_entry.context if used_fallback and old_entry is not None else build_history_context(bars)
        retained = bars[-20:]
        state.result[code] = retained
        self._cache_history_result(state, code, retained, used_fallback)
        state.pending_entries[code] = _HistoryEntry(
            bars=retained,
            expires_at=self._monotonic()
            + (min(60.0, self._history_ttl_seconds) if used_fallback or not bars else self._history_ttl_seconds),
            source=old_entry.source if used_fallback and old_entry is not None else "eastmoney",
            context=context,
        )

    def _cache_history_result(
        self,
        state: _HistoryLoadState,
        code: str,
        bars: tuple[DailyBar, ...],
        used_fallback: bool,
    ) -> None:
        cache = self._runner.cache
        if cache is None:
            return
        self._runner.ensure_before_deadline(state.request.deadline)
        assert state.cache_observed_at is not None
        identity = self._history_cache_identity(code, state.cache_observed_at)
        if bars:
            cache.put(identity, bars, data_version=_history_version(bars), source_time=_history_source_time(bars))
            if used_fallback:
                cache.put_negative(identity, error_code="history_refresh_failed")
            return
        cache.put_negative(identity, error_code="history_no_data")

    def _commit_history_entries(self, state: _HistoryLoadState) -> None:
        self._runner.ensure_before_deadline(state.request.deadline)
        with self._lock:
            self._runner.ensure_before_deadline(state.request.deadline)
            for code, incoming in tuple(state.pending_entries.items()):
                current = self._history.get(code)
                if (
                    current is not None
                    and current.bars
                    and (not incoming.bars or _history_version(incoming.bars) < _history_version(current.bars))
                ):
                    if incoming.bars:
                        self._history_out_of_order_count += 1
                    state.pending_entries[code] = current
                    state.result[code] = current.bars
            self._history.update(state.pending_entries)
            self.trim(set(state.request.codes))

    def _mark_non_actionable_history(self, state: _HistoryLoadState) -> None:
        cache = self._runner.cache
        if cache is None:
            return
        assert state.cache_observed_at is not None
        with self._lock:
            sources = {code: entry.source for code in state.result if (entry := self._history.get(code)) is not None}
        for code, bars in state.result.items():
            if not bars or sources.get(code) == "tushare":
                continue
            identity = self._history_cache_identity(code, state.cache_observed_at)
            if not cache.is_actionable(identity, _history_source_time(bars)):
                _add_action_restriction(state.request.action_restrictions, code, "history_data_degraded")

    def _history_cache_identity(self, code: str, observed_at: datetime) -> CacheIdentity:
        return self._runner.cache_identity(
            "daily_history",
            "eastmoney",
            code,
            {"code": code, "days": 61, "retained_days": 20, "adjust": "qfq"},
            observed_at,
        )

    def cached(
        self,
        codes: Iterable[str],
        *,
        fresh_only: bool = False,
        action_restrictions: dict[str, set[str]] | None = None,
    ) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(codes)
        now = self._monotonic()
        result, observed_at = self._shared_cached_history(requested, fresh_only)
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
        self._mark_cached_history_actionability(result, history_sources, observed_at, action_restrictions)
        return result

    def _shared_cached_history(
        self,
        codes: Sequence[str],
        fresh_only: bool,
    ) -> tuple[dict[str, tuple[DailyBar, ...]], datetime | None]:
        result: dict[str, tuple[DailyBar, ...]] = {}
        cache = self._runner.cache
        if cache is None:
            return result, None
        observed_at = self._runner.wall_clock()
        for code in codes:
            lookup = cache.get(self._history_cache_identity(code, observed_at))
            if (
                lookup is not None
                and lookup.value is not None
                and (not fresh_only or lookup.state == "fresh" or lookup.retry_suppressed)
            ):
                result[code] = cast(tuple[DailyBar, ...], lookup.value)
        return result, observed_at

    def _mark_cached_history_actionability(
        self,
        result: Mapping[str, tuple[DailyBar, ...]],
        history_sources: Mapping[str, str],
        observed_at: datetime | None,
        action_restrictions: dict[str, set[str]] | None,
    ) -> None:
        cache = self._runner.cache
        if cache is not None and observed_at is not None:
            for code, bars in result.items():
                if not bars or history_sources.get(code) == "tushare":
                    continue
                identity = self._history_cache_identity(code, observed_at)
                if not cache.is_actionable(identity, _history_source_time(bars)):
                    _add_action_restriction(action_restrictions, code, "history_data_degraded")

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
                ordered = tuple(sorted(bars, key=lambda item: item.trade_date))[-61:]
                if not ordered or any(bar.adjustment is not PriceAdjustment.QFQ for bar in ordered):
                    if ordered:
                        self._history_error_count += 1
                    continue
                current = self._history.get(code)
                if current is None or not current.bars or ordered[-1].trade_date > current.bars[-1].trade_date:
                    self._history[code] = _HistoryEntry(
                        ordered[-20:],
                        expires_at,
                        source=source,
                        context=build_history_context(ordered),
                    )
            self.trim(set(bars_by_code))

    def summaries(
        self,
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
    ) -> Mapping[str, HistoryContext]:
        require_qfq_history(histories)
        summaries: dict[str, HistoryContext] = {}
        del observed_at
        for code, bars in histories.items():
            with self._lock:
                entry = self._history.get(code)
            if entry is not None and entry.bars == bars and entry.context is not None:
                summaries[code] = entry.context
            else:
                summaries[code] = build_history_context(bars)
        return summaries

    def status(self) -> HistoryStoreStatus:
        with self._lock:
            return HistoryStoreStatus(
                entries=len(self._history),
                raw_rows=sum(len(entry.bars) for entry in self._history.values()),
                profile_entries=sum(entry.context is not None for entry in self._history.values()),
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
