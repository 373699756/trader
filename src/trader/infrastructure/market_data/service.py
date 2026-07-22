"""Thread-safe feature-data service built from quote and history adapters."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from trader.application.cache import BoundedCache, build_cache_identity, request_fingerprint
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.workers import BoundedExecutor
from trader.domain.models import FeatureSnapshot, MarketQuote
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import StandardizedFeatureBuilder
from trader.infrastructure.market_data.gateway import MarketDataGateway
from trader.infrastructure.market_data.history import DailyBar, HistoryProfile, summarize_history_metrics
from trader.infrastructure.market_data.history_seed import DailyHistoryClient
from trader.infrastructure.market_data.service_candidates import (
    MarketCandidateMixin,
    _apply_action_restrictions,
)
from trader.infrastructure.market_data.service_current_quotes import MarketCurrentQuoteMixin
from trader.infrastructure.market_data.service_execution import MarketExecutionMixin
from trader.infrastructure.market_data.service_health import MarketHealthMixin
from trader.infrastructure.market_data.service_history import MarketHistoryMixin
from trader.infrastructure.market_data.service_history_warmup import MarketHistoryWarmupMixin
from trader.infrastructure.market_data.service_intraday import MarketIntradayMixin
from trader.infrastructure.market_data.service_models import _HistoryEntry, _IntradayEntry, _ResearchEntry
from trader.infrastructure.market_data.service_research import MarketResearchMixin
from trader.infrastructure.market_data.service_support import (
    _history_preload_codes,
    _normalize_codes,
    _quote_version,
)
from trader.infrastructure.market_data.service_tushare import MarketTushareMixin
from trader.infrastructure.market_data.tushare import TushareClient
from trader.infrastructure.persistence.runtime_json import RuntimeJsonWriter


class MarketFeatureService(
    MarketExecutionMixin,
    MarketHealthMixin,
    MarketCurrentQuoteMixin,
    MarketHistoryWarmupMixin,
    MarketTushareMixin,
    MarketResearchMixin,
    MarketIntradayMixin,
    MarketHistoryMixin,
    MarketCandidateMixin,
):
    def __init__(
        self,
        gateway: MarketDataGateway,
        history_client: DailyHistoryClient,
        feature_builder: StandardizedFeatureBuilder,
        *,
        research_client: AkshareResearchClient | None = None,
        intraday_client: EastmoneyClient | None = None,
        tushare_client: TushareClient | None = None,
        history_workers: int = 6,
        research_workers: int = 4,
        intraday_workers: int = 6,
        history_preload_limit: int = 360,
        history_warmup_batch_size: int = 30,
        history_ttl_seconds: float = 21_600,
        research_ttl_seconds: float = 600,
        research_circuit_breaker_failures: int = 3,
        research_circuit_breaker_seconds: float = 60,
        intraday_ttl_seconds: float = 45,
        intraday_batch_timeout_seconds: float = 3,
        intraday_cache_limit: int = 360,
        history_cache_limit: int = 360,
        research_cache_limit: int = 360,
        market_ttl_seconds: float = 30,
        research_cache_dir: Path | None = None,
        json_writer: RuntimeJsonWriter | None = None,
        worker_pool: BoundedExecutor | None = None,
        history_worker_pool: BoundedExecutor | None = None,
        source_lanes: SourceLaneRegistry | None = None,
        cache: BoundedCache[object] | None = None,
        source_contract_versions: Mapping[str, str] | None = None,
        config_version: str = "component-default",
        schema_version: str = "market-v15",
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._gateway = gateway
        self._history_client = history_client
        self._feature_builder = feature_builder
        self._research_client = research_client
        self._intraday_client = intraday_client
        self._tushare_client = tushare_client
        self._history_workers = max(1, history_workers)
        self._research_workers = max(1, research_workers)
        self._intraday_workers = max(1, intraday_workers)
        self._history_preload_limit = max(1, history_preload_limit)
        self._history_warmup_batch_size = max(1, history_warmup_batch_size)
        self._history_ttl_seconds = max(60.0, history_ttl_seconds)
        self._research_ttl_seconds = max(60.0, research_ttl_seconds)
        self._research_failure_limit = max(1, research_circuit_breaker_failures)
        self._research_breaker_seconds = max(0.1, research_circuit_breaker_seconds)
        self._intraday_ttl_seconds = max(1.0, intraday_ttl_seconds)
        self._intraday_batch_timeout_seconds = max(0.01, intraday_batch_timeout_seconds)
        self._intraday_cache_limit = max(1, intraday_cache_limit)
        self._history_cache_limit = max(1, history_cache_limit)
        self._research_cache_limit = max(1, research_cache_limit)
        self._market_ttl_seconds = max(1.0, market_ttl_seconds)
        self._research_cache_dir = research_cache_dir
        self._json_writer = json_writer
        self._worker_pool = worker_pool
        self._history_worker_pool = history_worker_pool
        self._source_lanes = source_lanes
        self._cache = cache
        self._source_contract_versions = dict(source_contract_versions or {"tushare": "tushare-component-v1"})
        self._config_version = config_version
        self._schema_version = schema_version
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
        self._research_planned_count = 0
        self._research_timeout_count = 0
        self._research_consecutive_failures = 0
        self._research_latencies_ms: deque[float] = deque(maxlen=256)
        self._research_latest_source_time: datetime | None = None
        self._research_last_error = ""
        self._research_open_until = 0.0
        self._research_half_open_probe = False
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
        self._history_warmup_universe: tuple[str, ...] = ()
        self._history_warmup_inflight: set[str] = set()
        self._history_warmup_planned_count = 0
        self._history_warmup_completed_count = 0
        self._history_warmup_failure_count = 0
        self._history_warmup_last_source = ""
        self._quote_out_of_order_count = 0
        self._research_out_of_order_count = 0
        self._history_out_of_order_count = 0
        self._intraday_out_of_order_count = 0
        self._tushare_reference_fields: dict[str, dict[str, float]] = {}
        self._tushare_reference_versions: dict[str, str] = {}
        self._tushare_reference_version_order: dict[str, tuple[datetime, datetime, str]] = {}

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
        quotes = tuple(
            self._run_data_task_until(
                deadline,
                False,
                self._gateway.fetch_market,
                observed_at=observed_at,
                force=force,
                deadline=deadline,
            )
        )
        history_codes = _history_preload_codes(quotes, self._history_preload_limit)
        if self._source_lanes is not None:
            self.schedule_history_warmup(history_codes, observed_at)
        action_restrictions: dict[str, set[str]] = {}
        histories = (
            self._load_histories(
                history_codes,
                deadline=deadline,
                action_restrictions=action_restrictions,
            )
            if self._source_lanes is None
            else self._cached_histories(
                history_codes,
                fresh_only=True,
                action_restrictions=action_restrictions,
            )
        )
        self._ensure_before_deadline(deadline)
        features = _apply_action_restrictions(
            self._feature_builder.build(
                quotes,
                histories,
                observed_at,
                history_summaries=self._history_summaries(histories, observed_at),
            ),
            action_restrictions,
        )
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
        action_restrictions: dict[str, set[str]] = {}
        histories = self._load_histories(normalized, action_restrictions=action_restrictions)
        research_observations = self._load_research(
            normalized,
            observed_at,
            include_structured=include_structured_research,
            action_restrictions=action_restrictions,
        )
        intraday_minutes = (
            self._load_intraday(
                normalized,
                observed_at,
                action_restrictions=action_restrictions,
            )
            if include_intraday_tail
            else None
        )
        features = self._build_candidate_features(
            quotes,
            histories,
            observed_at,
            research_observations=research_observations,
            intraday_minutes=intraday_minutes,
            action_restrictions=action_restrictions,
        )
        if include_intraday_tail:
            self._record_intraday_feature_coverage(normalized, features)
        return features

    def refresh_candidate_quotes(
        self,
        codes: Sequence[str],
        observed_at: datetime,
        *,
        force: bool = False,
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
                observed_at=observed_at,
                force=force,
                deadline=deadline,
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
        action_restrictions: dict[str, set[str]] = {}
        return self._build_candidate_features(
            resolved,
            self._cached_histories(normalized, action_restrictions=action_restrictions),
            observed_at,
            research_observations=self._cached_research(
                normalized,
                include_structured=False,
                action_restrictions=action_restrictions,
            ),
            intraday_minutes=None,
            action_restrictions=action_restrictions,
        )

    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]:
        with self._lock:
            quotes = tuple(feature.quote for feature in self._market_features)
        if not quotes:
            return ()
        action_restrictions: dict[str, set[str]] = {}
        histories = self._cached_histories(
            (quote.code for quote in quotes),
            action_restrictions=action_restrictions,
        )
        features = _apply_action_restrictions(
            self._feature_builder.build(
                quotes,
                histories,
                observed_at,
                history_summaries=self._history_summaries(histories, observed_at),
            ),
            action_restrictions,
        )
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

    def _history_summaries(
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
                config_version=self._config_version,
                schema_version=self._schema_version,
            )
            cached = self._cache.get(identity) if self._cache is not None else None
            if (
                cached is not None
                and isinstance(cached.value, HistoryProfile)
                and cached.state
                not in {
                    "negative",
                    "degraded",
                }
            ):
                summaries[code] = cached.value
                continue

            def load(bars: tuple[DailyBar, ...] = bars) -> HistoryProfile:
                return summarize_history_metrics(bars)

            if self._cache is None:
                summaries[code] = load()
                continue
            summary = self._cache.coalesce(identity, load)
            if not isinstance(summary, HistoryProfile):
                raise TypeError("history summary cache returned an invalid value")
            self._cache.put(
                identity,
                summary,
                data_version=f"history:{history_version}",
                source_time=observed_at,
            )
            summaries[code] = summary
        return summaries

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
        action_restrictions: dict[str, set[str]] = {}
        histories = self._cached_histories(normalized, action_restrictions=action_restrictions)
        research = self._cached_research(
            normalized,
            include_structured=include_structured_research,
            action_restrictions=action_restrictions,
        )
        intraday = (
            self._cached_intraday(normalized, action_restrictions=action_restrictions)
            if include_intraday_tail
            else None
        )
        features = self._build_candidate_features(
            quotes,
            histories,
            observed_at,
            research_observations=research,
            intraday_minutes=intraday,
            action_restrictions=action_restrictions,
        )
        if include_intraday_tail:
            self._record_intraday_feature_coverage(normalized, features)
        return features


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) else None


__all__ = ["MarketFeatureService"]
