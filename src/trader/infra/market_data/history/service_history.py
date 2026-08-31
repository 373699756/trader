"""Daily history cache and bounded loading operations."""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, as_completed
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import asdict, dataclass, fields, replace
from datetime import date, datetime, time
from typing import TYPE_CHECKING, ParamSpec, Protocol, TypedDict, TypeVar, cast
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.application.cache import CacheIdentity
from trader.application.ports.data_plane import DataPlaneUnavailableError, HistoricalFeatureRecord
from trader.application.ports.market import MarketDataDeadlineExceededError
from trader.application.ports.types import JsonInput, JsonObject, freeze_json_object
from trader.application.workers import BorrowExecutorOptions, BoundedExecutor, borrow_executor, submit_or_run_inline
from trader.domain.outcome.models import OutcomeBar
from trader.infra.market_data.history.history import (
    DailyBar,
    HistoryContext,
    HistoryProfile,
    PriceAdjustment,
    build_history_context,
    require_qfq_history,
)
from trader.infra.market_data.history.history_seed import DailyHistoryClient
from trader.infra.market_data.service.market_cache_identity import (
    _add_action_restriction,
    _history_version,
    _source_batch_identity,
)
from trader.infra.market_data.service.service_execution import MarketTaskRunner
from trader.infra.market_data.service.service_models import _HistoryEntry

_P = ParamSpec("_P")
_T = TypeVar("_T")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_HISTORY_SOURCE_LANE = "history"
_HISTORY_CACHE_RETENTION_DAYS = 20
_HISTORY_CACHE_LOOKBACK_DAYS = 61

_LOGGER = logging.getLogger(__name__)


class _HistoryDataPlane(Protocol):
    def save_historical_feature_recent_records(self, records: Sequence[HistoricalFeatureRecord]) -> None: ...

    def load_historical_feature_recent_records(
        self, codes: Sequence[str] | None = None
    ) -> tuple[HistoricalFeatureRecord, ...]: ...


@dataclass(frozen=True)
class HistoryCacheStatus:
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
    pending_full_entries: dict[str, tuple[DailyBar, ...]]


class HistoryCacheOptions(TypedDict):
    history_worker_pool: BoundedExecutor | None
    workers: int
    ttl_seconds: float
    capacity: int
    history_data_plane: _HistoryDataPlane | None
    monotonic: Callable[[], float]


class HistoryCache:
    def __init__(
        self,
        history_client: DailyHistoryClient,
        runner: MarketTaskRunner,
        **options: Unpack[HistoryCacheOptions],
    ) -> None:
        self._history_client = history_client
        self._runner = runner
        self._history_worker_pool = options["history_worker_pool"]
        self._history_workers = max(1, options["workers"])
        self._history_ttl_seconds = max(60.0, options["ttl_seconds"])
        self._history_cache_limit = max(1, options["capacity"])
        self._monotonic = options["monotonic"]
        self._history_data_plane = options["history_data_plane"]
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
        state = _HistoryLoadState(request, result, previous, cache_observed_at, {}, {})
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
            timed_out = False
            timeout = (
                None
                if request.deadline is None
                else max(0.0, (request.deadline - self._runner.wall_clock()).total_seconds())
            )
            try:
                for future in as_completed(futures, timeout=timeout):
                    self._runner.ensure_before_deadline(state.request.deadline)
                    code = futures[future]
                    self._consume_history_future(state, code, future)
                    self._commit_history_entries(state)
            except FutureTimeoutError:
                timed_out = True
                pending = tuple(future for future in futures if not future.done())
                for future in pending:
                    future.cancel()
                with self._lock:
                    self._history_error_count += len(pending)
                state.request = replace(request, deadline=None)
        self._commit_history_entries(state)
        if timed_out:
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
            bars = tuple(sorted(future.result(), key=lambda item: item.trade_date))[-_HISTORY_CACHE_LOOKBACK_DAYS:]
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
        retained = bars[-_HISTORY_CACHE_RETENTION_DAYS:]
        if used_fallback:
            state.pending_full_entries.pop(code, None)
        else:
            state.pending_full_entries[code] = bars
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
        persist_candidates: list[tuple[str, tuple[DailyBar, ...], _HistoryEntry]] = []
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
                    state.pending_full_entries.pop(code, None)
            self._history.update(state.pending_entries)
            self.trim(set(state.request.codes))
            for code, incoming in state.pending_entries.items():
                full_bars = state.pending_full_entries.get(code)
                if full_bars:
                    persist_candidates.append((code, full_bars, incoming))
            state.pending_entries.clear()
            state.pending_full_entries.clear()
        for code, bars, entry in persist_candidates:
            self._persist_history_bars(code, bars, entry.context, entry.expires_at, entry.source)

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
        result, observed_at, degraded_codes = self._shared_cached_history(requested, fresh_only)
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
        self._mark_cached_history_actionability(
            result,
            history_sources,
            observed_at,
            degraded_codes,
            action_restrictions,
        )
        return result

    def _shared_cached_history(
        self,
        codes: Sequence[str],
        fresh_only: bool,
    ) -> tuple[dict[str, tuple[DailyBar, ...]], datetime | None, set[str]]:
        result: dict[str, tuple[DailyBar, ...]] = {}
        degraded_codes: set[str] = set()
        cache = self._runner.cache
        if cache is None:
            return result, None, degraded_codes
        observed_at = self._runner.wall_clock()
        for code in codes:
            identity = self._history_cache_identity(code, observed_at)
            lookup = cache.get(identity)
            if lookup is None or lookup.value is None or lookup.source_time is None:
                continue
            actionable = cache.is_actionable(identity, lookup.source_time)
            if fresh_only and (not actionable or (lookup.state != "fresh" and not lookup.retry_suppressed)):
                continue
            result[code] = cast(tuple[DailyBar, ...], lookup.value)
            if lookup.state != "fresh" or not actionable:
                degraded_codes.add(code)
        return result, observed_at, degraded_codes

    def _mark_cached_history_actionability(
        self,
        result: Mapping[str, tuple[DailyBar, ...]],
        history_sources: Mapping[str, str],
        observed_at: datetime | None,
        degraded_codes: set[str],
        action_restrictions: dict[str, set[str]] | None,
    ) -> None:
        for code in degraded_codes:
            _add_action_restriction(action_restrictions, code, "history_data_degraded")
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
        persist_candidates: list[tuple[str, tuple[DailyBar, ...], _HistoryEntry]] = []
        with self._lock:
            for code, bars in bars_by_code.items():
                ordered = tuple(sorted(bars, key=lambda item: item.trade_date))[-_HISTORY_CACHE_LOOKBACK_DAYS:]
                if not ordered or any(bar.adjustment is not PriceAdjustment.QFQ for bar in ordered):
                    if ordered:
                        self._history_error_count += 1
                    continue
                current = self._history.get(code)
                if current is None or not current.bars or ordered[-1].trade_date > current.bars[-1].trade_date:
                    entry = _HistoryEntry(
                        ordered[-_HISTORY_CACHE_RETENTION_DAYS:],
                        expires_at,
                        source=source,
                        context=build_history_context(ordered),
                    )
                    self._history[code] = entry
                    persist_candidates.append((code, ordered, entry))
            self.trim(set(bars_by_code))
        for code, bars, entry in persist_candidates:
            self._persist_history_bars(code, bars, entry.context, entry.expires_at, source)

    def recover_from_data_plane(self) -> None:
        data_plane = self._history_data_plane
        if data_plane is None:
            return
        try:
            records = data_plane.load_historical_feature_recent_records()
        except DataPlaneUnavailableError:
            _LOGGER.warning("history data plane unavailable during recovery")
            return
        except Exception as exc:
            _LOGGER.warning("history recovery read failed: %s", type(exc).__name__)
            return

        grouped, persisted_contexts = _restore_history_records(records)
        if not grouped:
            return
        expires_at = self._monotonic() + self._history_ttl_seconds
        with self._lock:
            for code, by_trade_date in grouped.items():
                ordered = tuple(sorted(by_trade_date.values(), key=lambda item: item.trade_date))
                if not ordered:
                    continue
                retained = ordered[-_HISTORY_CACHE_RETENTION_DAYS:]
                full = ordered[-_HISTORY_CACHE_LOOKBACK_DAYS:]
                persisted_context = persisted_contexts.get(code)
                if (
                    persisted_context is None
                    or persisted_context.latest_trade_date != ordered[-1].trade_date
                    or _requires_tomorrow_model_context_rebuild(persisted_context, full)
                ):
                    persisted_context = build_history_context(full)
                self._history[code] = _HistoryEntry(
                    bars=retained,
                    expires_at=expires_at,
                    source=retained[-1].source if retained else "eastmoney",
                    context=persisted_context,
                )
            self.trim(set(grouped))

    def _persist_history_bars(
        self,
        code: str,
        bars: tuple[DailyBar, ...],
        _context: HistoryContext | None,
        _expires_at: float,
        source: str,
    ) -> None:
        del _expires_at
        data_plane = self._history_data_plane
        if data_plane is None:
            return
        observed_at = self._runner.wall_clock()
        records: list[HistoricalFeatureRecord] = []
        for bar in bars:
            payload = dict(_serialize_daily_bar(bar))
            if _context is not None and bar.trade_date == _context.latest_trade_date:
                context_input = cast(dict[str, JsonInput], asdict(_context))
                payload["history_summary"] = freeze_json_object(context_input)
            records.append(
                HistoricalFeatureRecord(
                    code=code,
                    trade_date=bar.trade_date,
                    observed_at=observed_at,
                    source_time=min(_history_source_time((bar,)), observed_at),
                    source=source,
                    data_version=_history_version(bars),
                    payload=payload,
                )
            )
        try:
            data_plane.save_historical_feature_recent_records(records)
        except DataPlaneUnavailableError:
            _LOGGER.warning("history persistence unavailable for %s", code)
        except Exception:
            _LOGGER.exception("history persistence failed for %s", code)

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

    def status(self) -> HistoryCacheStatus:
        with self._lock:
            return HistoryCacheStatus(
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


def _serialize_daily_bar(bar: DailyBar) -> JsonObject:
    return {
        "trade_date": bar.trade_date,
        "open_price": bar.open_price,
        "close": bar.close,
        "high": bar.high,
        "low": bar.low,
        "volume": bar.volume,
        "amount": bar.amount,
        "pct_change": bar.pct_change,
        "turnover_rate": bar.turnover_rate,
        "adjustment": bar.adjustment.value,
        "source": bar.source,
    }


def _deserialize_daily_bar(payload: JsonObject) -> DailyBar:
    trade_date = _require_string(payload, "trade_date")
    open_price = _require_float(payload, "open_price")
    close = _require_float(payload, "close")
    high = _require_float(payload, "high")
    low = _require_float(payload, "low")
    volume = _require_float(payload, "volume")
    amount = _require_float(payload, "amount")
    pct_change = _require_float(payload, "pct_change")
    turnover_rate = _require_float_or_none(payload, "turnover_rate")
    if not isinstance(payload.get("adjustment"), str):
        raise TypeError("adjustment must be a string")
    if payload["adjustment"] not in {PriceAdjustment.QFQ.value, PriceAdjustment.RAW.value}:
        raise ValueError("adjustment must be qfq/raw")
    source = _require_string(payload, "source")
    return DailyBar(
        trade_date=trade_date,
        open_price=open_price,
        close=close,
        high=high,
        low=low,
        volume=volume,
        amount=amount,
        pct_change=pct_change,
        turnover_rate=turnover_rate,
        adjustment=PriceAdjustment(payload["adjustment"]),
        source=source,
    )


def _restore_history_records(
    records: Sequence[HistoricalFeatureRecord],
) -> tuple[dict[str, dict[str, DailyBar]], dict[str, HistoryContext]]:
    grouped: dict[str, dict[str, DailyBar]] = {}
    persisted_contexts: dict[str, HistoryContext] = {}
    for record in records:
        try:
            bar = _deserialize_daily_bar(record.payload)
        except Exception as exc:
            _LOGGER.warning(
                "history payload invalid for %s on %s: %s",
                record.code,
                record.trade_date,
                type(exc).__name__,
            )
            continue
        grouped.setdefault(record.code, {})[bar.trade_date] = bar
        context = _deserialize_history_context(record.payload.get("history_summary"))
        if context is None or context.latest_trade_date != bar.trade_date:
            continue
        current = persisted_contexts.get(record.code)
        if current is None or context.latest_trade_date > current.latest_trade_date:
            persisted_contexts[record.code] = context
    return grouped, persisted_contexts


def _deserialize_history_context(payload: object) -> HistoryContext | None:
    if not isinstance(payload, Mapping):
        return None
    profile = _deserialize_history_profile(payload.get("profile"))
    if profile is None:
        return None
    raw_previous = payload.get("previous_profile")
    previous = None if raw_previous is None else _deserialize_history_profile(raw_previous)
    if raw_previous is not None and previous is None:
        return None
    latest_trade_date = payload.get("latest_trade_date")
    sample_count = payload.get("sample_count")
    anchors = _deserialize_return_anchors(payload.get("return_anchors"))
    if (
        not isinstance(latest_trade_date, str)
        or not latest_trade_date
        or not isinstance(sample_count, int)
        or isinstance(sample_count, bool)
        or sample_count < 0
        or anchors is None
    ):
        return None
    return HistoryContext(
        profile=profile,
        previous_profile=previous,
        latest_trade_date=latest_trade_date,
        sample_count=sample_count,
        return_anchors=anchors,
    )


def _requires_tomorrow_model_context_rebuild(
    context: HistoryContext,
    bars: tuple[DailyBar, ...],
) -> bool:
    if len(bars) < _HISTORY_CACHE_LOOKBACK_DAYS:
        return False
    anchors = dict(context.return_anchors)
    return (
        any(days not in anchors for days in (1, 3, 5, 20, 40, 60))
        or context.profile.average_amount_20d is None
        or context.profile.amihud_20d is None
    )


def _deserialize_history_profile(payload: object) -> HistoryProfile | None:
    if not isinstance(payload, Mapping):
        return None
    values: dict[str, float | None] = {}
    for profile_field in fields(HistoryProfile):
        raw_value = payload.get(profile_field.name)
        if raw_value is None:
            values[profile_field.name] = None
            continue
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
            return None
        numeric = float(raw_value)
        if not math.isfinite(numeric):
            return None
        values[profile_field.name] = numeric
    return HistoryProfile(**values)


def _deserialize_return_anchors(payload: object) -> tuple[tuple[int, float], ...] | None:
    if not isinstance(payload, (tuple, list)):
        return None
    anchors: list[tuple[int, float]] = []
    for item in payload:
        if not isinstance(item, (tuple, list)) or len(item) != 2:
            return None
        days, raw_price = item
        if (
            not isinstance(days, int)
            or isinstance(days, bool)
            or days <= 0
            or isinstance(raw_price, bool)
            or not isinstance(raw_price, (int, float))
        ):
            return None
        price = float(raw_price)
        if not math.isfinite(price) or price <= 0:
            return None
        anchors.append((days, price))
    return tuple(anchors)


def _require_string(payload: JsonObject, key: str) -> str:
    raw = payload.get(key)
    if not isinstance(raw, str) or not raw:
        raise TypeError(f"{key} must be a non-empty string")
    return raw


def _require_float(payload: JsonObject, key: str) -> float:
    raw = payload.get(key)
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise TypeError(f"{key} must be a finite number")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return value


def _require_float_or_none(payload: JsonObject, key: str) -> float | None:
    raw = payload.get(key)
    if raw is None:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        raise TypeError(f"{key} must be a number or null")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    return value


def _history_source_time(bars: Sequence[DailyBar]) -> datetime:
    latest = date.fromisoformat(_history_version(bars))
    return datetime.combine(latest, time(15, 0), _SHANGHAI)
