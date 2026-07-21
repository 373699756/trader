"""Thread-safe feature-data service built from quote and history adapters."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from pathlib import Path
from typing import ParamSpec, TypeVar

from trader.application.ports import MarketDataDeadlineExceeded
from trader.application.workers import BoundedExecutor
from trader.domain.models import FeatureSnapshot, MarketQuote
from trader.domain.research import ResearchObservation
from trader.domain.tail import MinuteBar
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import StandardizedFeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar
from trader.infrastructure.market_data.service_history import MarketHistoryMixin
from trader.infrastructure.market_data.service_intraday import MarketIntradayMixin
from trader.infrastructure.market_data.service_models import _HistoryEntry, _IntradayEntry, _ResearchEntry
from trader.infrastructure.market_data.service_research import MarketResearchMixin
from trader.infrastructure.market_data.service_support import (
    _history_preload_codes,
    _normalize_codes,
    _quote_age_summary,
    _quote_version,
)
from trader.infrastructure.persistence.runtime_json import RuntimeJsonWriter

_P = ParamSpec("_P")
_T = TypeVar("_T")


class MarketFeatureService(MarketResearchMixin, MarketIntradayMixin, MarketHistoryMixin):
    def __init__(
        self,
        gateway: MarketDataGateway,
        history_client: EastmoneyClient,
        feature_builder: StandardizedFeatureBuilder,
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
        research_cache_dir: Path | None = None,
        json_writer: RuntimeJsonWriter | None = None,
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
        self._research_cache_dir = research_cache_dir
        self._json_writer = json_writer
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
        quotes = tuple(self._run_data_task_until(deadline, False, self._gateway.fetch_market))
        history_codes = _history_preload_codes(quotes, self._history_preload_limit)
        histories = self._load_histories(history_codes, deadline=deadline)
        self._ensure_before_deadline(deadline)
        features = self._feature_builder.build(quotes, histories, observed_at)
        self._ensure_before_deadline(deadline)
        with self._lock:
            self._ensure_before_deadline(deadline)
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
        quotes = tuple(
            self._run_data_task_until(
                deadline,
                True,
                self._gateway.fetch_candidates,
                normalized,
            )
        )
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
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        pool = self._worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            return function(*args, **kwargs)
        submit = pool.submit_urgent if urgent else pool.submit
        future = submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected source task")
        return future.result()

    def _run_data_task_until(
        self,
        deadline: datetime | None,
        urgent: bool,
        function: Callable[_P, _T],
        /,
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _T:
        if deadline is None:
            return self._run_data_task(urgent, function, *args, **kwargs)
        self._ensure_before_deadline(deadline)
        pool = self._worker_pool
        if pool is None or not pool.is_running() or pool.owns_current_thread():
            result = function(*args, **kwargs)
            self._ensure_before_deadline(deadline)
            return result
        submit = pool.submit_urgent if urgent else pool.submit
        future = submit(function, *args, **kwargs)
        if future is None:
            raise RuntimeError("data worker queue rejected deadline-bound source task")
        remaining = max(0.0, (deadline - self._wall_clock()).total_seconds())
        try:
            result = future.result(timeout=remaining)
        except FutureTimeoutError as exc:
            future.cancel()
            raise MarketDataDeadlineExceeded("data source task exceeded its batch deadline") from exc
        self._ensure_before_deadline(deadline)
        return result

    def _ensure_before_deadline(self, deadline: datetime | None) -> None:
        if deadline is not None and self._wall_clock() >= deadline:
            raise MarketDataDeadlineExceeded("market-data result completed after its batch deadline")

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


__all__ = ["MarketFeatureService"]
