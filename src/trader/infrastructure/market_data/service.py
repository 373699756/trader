"""Thread-safe feature-data service built from quote and history adapters."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from concurrent.futures import as_completed, wait
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import ParamSpec, TypeVar

from trader.application.workers import BoundedExecutor, borrow_executor
from trader.domain.models import FeatureSnapshot, MarketQuote
from trader.domain.research import ResearchObservation
from trader.domain.tail import TAIL_SIGNAL_VALUE_FIELDS, MinuteBar
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import FeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(frozen=True)
class _HistoryEntry:
    bars: tuple[DailyBar, ...]
    expires_at: float


@dataclass(frozen=True)
class _ResearchEntry:
    observation: ResearchObservation
    expires_at: float


@dataclass(frozen=True)
class _IntradayEntry:
    bars: tuple[MinuteBar, ...]
    expires_at: float


class MarketFeatureService:
    def __init__(
        self,
        gateway: MarketDataGateway,
        history_client: EastmoneyClient,
        feature_builder: FeatureBuilder,
        *,
        research_client: AkshareResearchClient | None = None,
        intraday_client: EastmoneyClient | None = None,
        history_workers: int = 6,
        research_workers: int = 4,
        intraday_workers: int = 6,
        history_preload_limit: int = 360,
        history_ttl_seconds: float = 21_600,
        research_ttl_seconds: float = 600,
        intraday_ttl_seconds: float = 45,
        intraday_batch_timeout_seconds: float = 3,
        intraday_cache_limit: int = 360,
        market_ttl_seconds: float = 30,
        worker_pool: BoundedExecutor | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._gateway = gateway
        self._history_client = history_client
        self._feature_builder = feature_builder
        self._research_client = research_client
        self._intraday_client = intraday_client
        self._history_workers = max(1, history_workers)
        self._research_workers = max(1, research_workers)
        self._intraday_workers = max(1, intraday_workers)
        self._history_preload_limit = max(1, history_preload_limit)
        self._history_ttl_seconds = max(60.0, history_ttl_seconds)
        self._research_ttl_seconds = max(60.0, research_ttl_seconds)
        self._intraday_ttl_seconds = max(1.0, intraday_ttl_seconds)
        self._intraday_batch_timeout_seconds = max(0.01, intraday_batch_timeout_seconds)
        self._intraday_cache_limit = max(1, intraday_cache_limit)
        self._market_ttl_seconds = max(1.0, market_ttl_seconds)
        self._worker_pool = worker_pool
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._market_features: tuple[FeatureSnapshot, ...] = ()
        self._market_expires_at = 0.0
        self._candidate_quotes: dict[str, MarketQuote] = {}
        self._history: dict[str, _HistoryEntry] = {}
        self._research: dict[tuple[str, bool], _ResearchEntry] = {}
        self._intraday: dict[str, _IntradayEntry] = {}
        self._research_success_count = 0
        self._research_error_count = 0
        self._research_last_error = ""
        self._intraday_success_count = 0
        self._intraday_error_count = 0
        self._intraday_last_error = ""
        self._intraday_requested_rows = 0
        self._intraday_covered_rows = 0
        self._intraday_latest_source_time = ""
        self._intraday_sources: tuple[str, ...] = ()
        self._intraday_data_versions: tuple[str, ...] = ()
        self._history_universe_rows = 0
        self._history_covered_rows = 0
        self._history_error_count = 0
        self._history_data_versions: tuple[str, ...] = ()
        self._quote_out_of_order_count = 0
        self._research_out_of_order_count = 0
        self._history_out_of_order_count = 0
        self._intraday_out_of_order_count = 0

    def fetch_market_features(
        self,
        observed_at: datetime,
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        now = self._monotonic()
        with self._lock:
            if not force and self._market_features and self._market_expires_at > now:
                return self._market_features
        quotes = tuple(self._run_data_task_until(deadline, self._gateway.fetch_market))
        history_codes = _history_preload_codes(quotes, self._history_preload_limit)
        histories = self._load_histories(history_codes)
        features = self._feature_builder.build(quotes, histories, observed_at)
        with self._lock:
            self._market_features = features
            self._market_expires_at = self._monotonic() + self._market_ttl_seconds
            self._history_universe_rows = len(history_codes)
            self._history_covered_rows = sum(len(histories.get(code, ())) >= 20 for code in history_codes)
            self._history_data_versions = tuple(sorted({quote.data_version for quote in quotes}))
        return features

    def fetch_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        self.refresh_candidate_quotes(normalized, observed_at)
        quotes = self._candidate_quote_snapshot(normalized)
        if {quote.code for quote in quotes} != set(normalized):
            self.fetch_market_features(observed_at)
            quotes = self._candidate_quote_snapshot(normalized)
        histories = self._load_histories(normalized)
        research_observations = self._load_research(
            normalized,
            observed_at,
            include_structured=include_structured_research,
        )
        intraday_minutes = self._load_intraday(normalized, observed_at) if include_intraday_tail else None
        features = self._build_candidate_features(
            quotes,
            histories,
            observed_at,
            research_observations=research_observations,
            intraday_minutes=intraday_minutes,
        )
        if include_intraday_tail:
            self._record_intraday_feature_coverage(normalized, features)
        return features

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        quotes = tuple(self._run_data_task_until(deadline, self._gateway.fetch_candidates, normalized))
        with self._lock:
            market_quotes = {feature.quote.code: feature.quote for feature in self._market_features}
            for quote in quotes:
                available = tuple(
                    item
                    for item in (self._candidate_quotes.get(quote.code), market_quotes.get(quote.code))
                    if item is not None
                )
                current = max(available, key=_quote_version) if available else None
                if current is not None and _quote_version(quote) < _quote_version(current):
                    self._quote_out_of_order_count += 1
                    continue
                self._candidate_quotes[quote.code] = quote
            excess = len(self._candidate_quotes) - self._intraday_cache_limit
            if excess > 0:
                for code in sorted(
                    self._candidate_quotes,
                    key=lambda item: (_quote_version(self._candidate_quotes[item]), item),
                )[:excess]:
                    self._candidate_quotes.pop(code, None)
        resolved = self._candidate_quote_snapshot(normalized)
        return self._build_candidate_features(
            resolved,
            self._cached_histories(normalized),
            observed_at,
            research_observations=self._cached_research(normalized, include_structured=False),
            intraday_minutes=None,
        )

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        with self._lock:
            quotes = tuple(feature.quote for feature in self._market_features)
        if not quotes:
            return ()
        histories = self._cached_histories(quote.code for quote in quotes)
        features = self._feature_builder.build(quotes, histories, observed_at)
        with self._lock:
            self._market_features = features
        return features

    def refresh_market_news(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None:
        normalized = _normalize_codes(codes)
        self._load_research(
            normalized,
            observed_at,
            include_structured=False,
            force=True,
            deadline=deadline,
        )

    def refresh_stock_risk(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        deadline: datetime | None = None,
    ) -> None:
        normalized = _normalize_codes(codes)
        self._load_research(
            normalized,
            observed_at,
            include_structured=True,
            force=True,
            deadline=deadline,
        )

    def refresh_reference_data(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
    ) -> None:
        del observed_at
        normalized = _normalize_codes(codes)
        self._load_histories(normalized, force=force)

    def refresh_intraday_tail(self, codes: Sequence[str], observed_at: datetime) -> None:
        self._load_intraday(_normalize_codes(codes), observed_at)

    def read_candidate_features(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_intraday_tail: bool = False,
        include_structured_research: bool = False,
    ) -> Sequence[FeatureSnapshot]:
        normalized = _normalize_codes(codes)
        if not normalized:
            return ()
        quotes = self._candidate_quote_snapshot(normalized)
        histories = self._cached_histories(normalized)
        research = self._cached_research(normalized, include_structured=include_structured_research)
        intraday = self._cached_intraday(normalized) if include_intraday_tail else None
        features = self._build_candidate_features(
            quotes,
            histories,
            observed_at,
            research_observations=research,
            intraday_minutes=intraday,
        )
        if include_intraday_tail:
            self._record_intraday_feature_coverage(normalized, features)
        return features

    def health(self) -> Mapping[str, object]:
        measured_at = self._wall_clock()
        with self._lock:
            market_quotes = tuple(feature.quote for feature in self._market_features)
            candidate_quotes = tuple(self._candidate_quotes.values())
            history_entries = len(self._history)
            market_cached = len(self._market_features)
            candidate_cached = len(self._candidate_quotes)
            research_entries = len(self._research)
            intraday_entries = len(self._intraday)
            research_success_count = self._research_success_count
            research_error_count = self._research_error_count
            research_last_error = self._research_last_error
            intraday_success_count = self._intraday_success_count
            intraday_error_count = self._intraday_error_count
            intraday_last_error = self._intraday_last_error
            intraday_requested_rows = self._intraday_requested_rows
            intraday_covered_rows = self._intraday_covered_rows
            intraday_latest_source_time = self._intraday_latest_source_time
            intraday_sources = self._intraday_sources
            intraday_data_versions = self._intraday_data_versions
            history_universe_rows = self._history_universe_rows
            history_covered_rows = self._history_covered_rows
            history_error_count = self._history_error_count
            history_data_versions = self._history_data_versions
            quote_out_of_order_count = self._quote_out_of_order_count
            research_out_of_order_count = self._research_out_of_order_count
            history_out_of_order_count = self._history_out_of_order_count
            intraday_out_of_order_count = self._intraday_out_of_order_count
        return {
            **dict(self._gateway.health()),
            "history_cache_entries": history_entries,
            "market_feature_rows": market_cached,
            "candidate_quote_cache_entries": candidate_cached,
            "research_cache_entries": research_entries,
            "research_success_count": research_success_count,
            "research_error_count": research_error_count,
            "research_last_error": research_last_error,
            "intraday_tail_cache_entries": intraday_entries,
            "intraday_tail_success_count": intraday_success_count,
            "intraday_tail_error_count": intraday_error_count,
            "intraday_tail_last_error": intraday_last_error,
            "intraday_tail_requested_rows": intraday_requested_rows,
            "intraday_tail_covered_rows": intraday_covered_rows,
            "intraday_tail_coverage_ratio": intraday_covered_rows / intraday_requested_rows
            if intraday_requested_rows
            else 0.0,
            "intraday_tail_latest_source_time": intraday_latest_source_time,
            "intraday_tail_sources": intraday_sources,
            "intraday_tail_data_versions": intraday_data_versions,
            "history_universe_rows": history_universe_rows,
            "history_covered_rows": history_covered_rows,
            "history_coverage_ratio": history_covered_rows / history_universe_rows if history_universe_rows else 0.0,
            "history_error_count": history_error_count,
            "history_data_versions": history_data_versions,
            "quote_out_of_order_count": quote_out_of_order_count,
            "research_out_of_order_count": research_out_of_order_count,
            "history_out_of_order_count": history_out_of_order_count,
            "intraday_out_of_order_count": intraday_out_of_order_count,
            "market_quote_age": _quote_age_summary(market_quotes, measured_at),
            "candidate_quote_age": _quote_age_summary(candidate_quotes, measured_at),
            "measured_at": measured_at.isoformat(),
        }

    def _run_data_task(
        self,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        pool = self._worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            return function(*args, **kwargs)
        future = pool.submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected source task")
        return future.result()

    def _run_data_task_until(
        self,
        deadline: datetime | None,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        if deadline is None:
            return self._run_data_task(function, *args, **kwargs)
        pool = self._worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            return function(*args, **kwargs)
        future = pool.submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected deadline-bound source task")
        remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
        try:
            return future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError("data source task exceeded its batch deadline") from exc

    def _candidate_quote_snapshot(self, codes: Sequence[str]) -> tuple[MarketQuote, ...]:
        with self._lock:
            market = {feature.quote.code: feature.quote for feature in self._market_features}
            result: list[MarketQuote] = []
            for code in codes:
                targeted = self._candidate_quotes.get(code)
                full_market = market.get(code)
                available = tuple(quote for quote in (targeted, full_market) if quote is not None)
                if available:
                    result.append(max(available, key=_quote_version))
            return tuple(result)

    def _build_candidate_features(
        self,
        quotes: Sequence[MarketQuote],
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
        *,
        research_observations: Mapping[str, ResearchObservation],
        intraday_minutes: Mapping[str, Sequence[MinuteBar]] | None,
    ) -> tuple[FeatureSnapshot, ...]:
        with self._lock:
            cross_section_reference = {feature.quote.code: feature.values for feature in self._market_features}
            cross_section_normalization_reference = {
                feature.quote.code: feature.normalization for feature in self._market_features
            }
        return self._feature_builder.build(
            quotes,
            histories,
            observed_at,
            cross_section_reference=cross_section_reference,
            cross_section_normalization_reference=cross_section_normalization_reference,
            research_observations=research_observations,
            intraday_minutes=intraday_minutes,
        )

    def _cached_research(
        self,
        codes: Sequence[str],
        *,
        include_structured: bool,
    ) -> Mapping[str, ResearchObservation]:
        now = self._monotonic()
        with self._lock:
            result: dict[str, ResearchObservation] = {}
            for code in codes:
                entry = self._research.get((code, include_structured))
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.observation
            return result

    def _cached_intraday(self, codes: Sequence[str]) -> Mapping[str, tuple[MinuteBar, ...]]:
        now = self._monotonic()
        with self._lock:
            result: dict[str, tuple[MinuteBar, ...]] = {}
            for code in codes:
                entry = self._intraday.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.bars
            return result

    def _load_research(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        include_structured: bool,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Mapping[str, ResearchObservation]:
        if self._research_client is None:
            return {}
        now = self._monotonic()
        result: dict[str, ResearchObservation] = {}
        previous: dict[str, _ResearchEntry] = {}
        with self._lock:
            for code in codes:
                entry = self._research.get((code, include_structured))
                if entry is None:
                    continue
                previous[code] = entry
                if not force and entry.expires_at > now:
                    result[code] = entry.observation
        missing = [code for code in codes if force or code not in result]
        if not missing:
            return result
        with borrow_executor(
            self._worker_pool,
            worker_count=min(self._research_workers, len(missing)),
            thread_name_prefix="candidate-research",
            queue_capacity=len(missing),
            wait_on_exit=deadline is None,
        ) as pool:
            futures = {}
            for code in missing:
                future = pool.submit(
                    self._fetch_research_observation,
                    code,
                    observed_at,
                    include_structured=include_structured,
                )
                if future is None:
                    raise RuntimeError("data worker queue rejected research task")
                futures[future] = code
            timeout = None if deadline is None else max(0.0, (deadline - self._wall_clock()).total_seconds())
            completed, pending = wait(futures, timeout=timeout)
            for future in completed:
                code = futures[future]
                ttl = self._research_ttl_seconds
                old_entry = previous.get(code)
                try:
                    observation = future.result()
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    observation = _degraded_research_observation(old_entry, str(exc))
                    ttl = min(60.0, ttl)
                    with self._lock:
                        self._research_error_count += 1
                        self._research_last_error = str(exc)[:240]
                else:
                    if _research_is_older(observation, old_entry):
                        observation = _degraded_research_observation(old_entry, "out_of_order_research_result")
                        ttl = min(60.0, ttl)
                        with self._lock:
                            self._research_out_of_order_count += 1
                    else:
                        observation = _merge_research_observation(old_entry, observation)
                    with self._lock:
                        self._research_success_count += 1
                        if observation.source_errors:
                            self._research_error_count += len(observation.source_errors)
                            self._research_last_error = observation.source_errors[-1][:240]
                            ttl = min(60.0, ttl)
                result[code] = observation
                with self._lock:
                    self._research[(code, include_structured)] = _ResearchEntry(
                        observation,
                        self._monotonic() + ttl,
                    )
            for future in pending:
                code = futures[future]
                future.cancel()
                observation = _degraded_research_observation(previous.get(code), "research_batch_deadline")
                result[code] = observation
                with self._lock:
                    self._research_error_count += 1
                    self._research_last_error = "research_batch_deadline"
                    self._research[(code, include_structured)] = _ResearchEntry(
                        observation,
                        self._monotonic() + min(60.0, self._research_ttl_seconds),
                    )
        return result

    def _fetch_research_observation(
        self,
        code: str,
        observed_at: datetime,
        *,
        include_structured: bool,
    ) -> ResearchObservation:
        if self._research_client is None:
            return ResearchObservation()
        if include_structured:
            return self._research_client.fetch_snapshot(code, observed_at=observed_at)
        return ResearchObservation(evidence=tuple(self._research_client.fetch_news(code, observed_at=observed_at)))

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

    def _load_histories(
        self,
        codes: Sequence[str],
        *,
        force: bool = False,
    ) -> Mapping[str, tuple[DailyBar, ...]]:
        result = {} if force else self._cached_histories(codes)
        with self._lock:
            previous = {code: self._history[code] for code in codes if code in self._history}
        missing = [code for code in codes if force or code not in result]
        if not missing:
            return result
        with borrow_executor(
            self._worker_pool,
            worker_count=min(self._history_workers, len(missing)),
            thread_name_prefix="candidate-history",
            queue_capacity=len(missing),
        ) as pool:
            futures = {}
            for code in missing:
                future = pool.submit(self._history_client.fetch_history, code, days=90)
                if future is None:
                    raise RuntimeError("data worker queue rejected history task")
                futures[future] = code
            for future in as_completed(futures):
                code = futures[future]
                old_entry = previous.get(code)
                used_fallback = False
                try:
                    bars = tuple(future.result())
                except Exception:
                    bars = ()
                    with self._lock:
                        self._history_error_count += 1
                if bars and old_entry is not None and _history_version(bars) < _history_version(old_entry.bars):
                    bars = old_entry.bars
                    used_fallback = True
                    with self._lock:
                        self._history_out_of_order_count += 1
                elif not bars and old_entry is not None and old_entry.bars:
                    bars = old_entry.bars
                    used_fallback = True
                result[code] = bars
                if bars:
                    with self._lock:
                        self._history[code] = _HistoryEntry(
                            bars=bars,
                            expires_at=self._monotonic()
                            + (min(60.0, self._history_ttl_seconds) if used_fallback else self._history_ttl_seconds),
                        )
                else:
                    with self._lock:
                        self._history[code] = _HistoryEntry(
                            bars=(),
                            expires_at=self._monotonic() + min(60.0, self._history_ttl_seconds),
                        )
        return result

    def _cached_histories(self, codes: Iterable[str]) -> dict[str, tuple[DailyBar, ...]]:
        requested = tuple(codes)
        now = self._monotonic()
        result: dict[str, tuple[DailyBar, ...]] = {}
        with self._lock:
            for code in requested:
                entry = self._history.get(code)
                if entry is None:
                    continue
                if entry.expires_at <= now:
                    continue
                result[code] = entry.bars
        return result


def _history_preload_codes(quotes: Sequence[MarketQuote], limit: int) -> tuple[str, ...]:
    groups: dict[str, list[MarketQuote]] = {}
    for quote in quotes:
        if quote.is_suspended or quote.price is None or not math.isfinite(quote.price) or quote.price <= 0:
            continue
        groups.setdefault(quote.industry or "unknown", []).append(quote)
    for group in groups.values():
        group.sort(key=_history_priority)
    representatives = sorted((group[0] for group in groups.values()), key=_history_priority)
    selected = representatives[:limit]
    selected_codes = {quote.code for quote in selected}
    remaining = sorted(
        (quote for group in groups.values() for quote in group if quote.code not in selected_codes),
        key=_history_priority,
    )
    selected.extend(remaining[: max(0, limit - len(selected))])
    return tuple(quote.code for quote in selected)


def _normalize_codes(codes: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))


def _quote_version(quote: MarketQuote) -> tuple[datetime, datetime, str]:
    return (quote.source_time, quote.received_time, quote.data_version)


def _quote_age_summary(quotes: Sequence[MarketQuote], measured_at: datetime) -> Mapping[str, object]:
    if not quotes:
        return {
            "sample_count": 0,
            "p50_seconds": None,
            "p95_seconds": None,
            "maximum_seconds": None,
            "latest_source_time": None,
        }
    ages = sorted(quote.age_seconds(measured_at) for quote in quotes)
    return {
        "sample_count": len(ages),
        "p50_seconds": round(ages[max(0, math.ceil(len(ages) * 0.50) - 1)], 3),
        "p95_seconds": round(ages[max(0, math.ceil(len(ages) * 0.95) - 1)], 3),
        "maximum_seconds": round(ages[-1], 3),
        "latest_source_time": max(quote.source_time for quote in quotes).isoformat(),
    }


def _minute_version(bars: Sequence[MinuteBar]) -> tuple[float, float, str]:
    return max(
        ((bar.source_time.timestamp(), bar.received_time.timestamp(), bar.data_version) for bar in bars),
        default=(float("-inf"), float("-inf"), ""),
    )


def _history_version(bars: Sequence[DailyBar]) -> str:
    return max((bar.trade_date for bar in bars), default="")


def _research_version(observation: ResearchObservation) -> tuple[float, float] | None:
    published = [item.published_at.timestamp() for item in observation.announcements]
    published.extend(item.published_at.timestamp() for item in observation.evidence)
    received = [item.received_at.timestamp() for item in observation.evidence if item.received_at is not None]
    if observation.financial is not None:
        published.append(observation.financial.published_at.timestamp())
    if not published:
        return None
    return (max(published), max(received, default=float("-inf")))


def _research_is_older(observation: ResearchObservation, old_entry: _ResearchEntry | None) -> bool:
    if old_entry is None:
        return False
    current_version = _research_version(observation)
    previous_version = _research_version(old_entry.observation)
    return current_version is not None and previous_version is not None and current_version < previous_version


def _degraded_research_observation(
    old_entry: _ResearchEntry | None,
    error: str,
) -> ResearchObservation:
    normalized_error = error[:240] or "research_refresh_failed"
    if old_entry is None:
        return ResearchObservation(source_errors=(normalized_error,))
    previous = old_entry.observation
    return replace(
        previous,
        source_errors=tuple(dict.fromkeys((*previous.source_errors, normalized_error))),
    )


def _merge_research_observation(
    old_entry: _ResearchEntry | None,
    current: ResearchObservation,
) -> ResearchObservation:
    if old_entry is None or not current.source_errors:
        return current
    previous = old_entry.observation
    failed_sources = {error.partition(":")[0] for error in current.source_errors}
    evidence = tuple({item.evidence_id: item for item in (*previous.evidence, *current.evidence)}.values())[-60:]
    return replace(
        current,
        financial=(
            previous.financial if "financial" in failed_sources and current.financial is None else current.financial
        ),
        announcements=(
            previous.announcements
            if "announcements" in failed_sources and not current.announcements_available
            else current.announcements
        ),
        announcements_available=(
            previous.announcements_available
            if "announcements" in failed_sources and not current.announcements_available
            else current.announcements_available
        ),
        pledge_ratio_pct=(
            previous.pledge_ratio_pct
            if "pledge" in failed_sources and current.pledge_ratio_pct is None
            else current.pledge_ratio_pct
        ),
        unlock_ratio_pct=(
            previous.unlock_ratio_pct
            if "unlock" in failed_sources and current.unlock_ratio_pct is None
            else current.unlock_ratio_pct
        ),
        evidence=evidence,
        source_errors=tuple(dict.fromkeys((*previous.source_errors, *current.source_errors))),
    )


def _history_priority(quote: MarketQuote) -> tuple[float, float, str]:
    return (
        -(quote.amount if quote.amount is not None and math.isfinite(quote.amount) else -1.0),
        -(abs(quote.pct_change) if quote.pct_change is not None and math.isfinite(quote.pct_change) else -1.0),
        quote.code,
    )


__all__ = ["MarketFeatureService"]
