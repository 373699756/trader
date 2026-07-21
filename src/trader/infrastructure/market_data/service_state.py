"""Typed shared state contract for MarketFeatureService mixins."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.application.cache import BoundedCache, CacheIdentity
from trader.application.source_lanes import SourceLaneRegistry
from trader.application.workers import BoundedExecutor
from trader.domain.models import FeatureSnapshot, MarketQuote
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.features import StandardizedFeatureBuilder
from trader.infrastructure.market_data.history import DailyBar, HistoryProfile
from trader.infrastructure.market_data.service_models import _HistoryEntry, _IntradayEntry, _ResearchEntry
from trader.infrastructure.market_data.tushare import TushareClient
from trader.infrastructure.persistence.runtime_json import RuntimeJsonWriter


class MarketServiceState:
    _gateway: Any
    _history_client: EastmoneyClient
    _feature_builder: StandardizedFeatureBuilder
    _research_client: AkshareResearchClient | None
    _intraday_client: EastmoneyClient | None
    _tushare_client: TushareClient | None
    _worker_pool: BoundedExecutor | None
    _source_lanes: SourceLaneRegistry | None
    _cache: BoundedCache[object] | None
    _source_contract_versions: dict[str, str]
    _config_version: str
    _schema_version: str
    _json_writer: RuntimeJsonWriter | None
    _research_cache_dir: Path | None
    _monotonic: Callable[[], float]
    _wall_clock: Callable[[], datetime]
    _lock: Any
    _history: dict[str, _HistoryEntry]
    _market_features: tuple[FeatureSnapshot, ...]
    _candidate_quotes: dict[str, MarketQuote]
    _research: dict[tuple[str, bool], _ResearchEntry]
    _intraday: dict[str, _IntradayEntry]
    _history_workers: int
    _research_workers: int
    _intraday_workers: int
    _history_ttl_seconds: float
    _research_ttl_seconds: float
    _intraday_ttl_seconds: float
    _intraday_batch_timeout_seconds: float
    _intraday_cache_limit: int
    _history_cache_limit: int
    _research_cache_limit: int
    _history_error_count: int
    _history_out_of_order_count: int
    _history_universe_rows: int
    _history_covered_rows: int
    _history_data_versions: tuple[str, ...]
    _quote_out_of_order_count: int
    _research_success_count: int
    _research_error_count: int
    _research_planned_count: int
    _research_timeout_count: int
    _research_consecutive_failures: int
    _research_latencies_ms: deque[float]
    _research_latest_source_time: datetime | None
    _research_last_error: str
    _research_failure_limit: int
    _research_breaker_seconds: float
    _research_open_until: float
    _research_half_open_probe: bool
    _research_out_of_order_count: int
    _intraday_success_count: int
    _intraday_error_count: int
    _intraday_last_error: str
    _intraday_out_of_order_count: int
    _intraday_requested_rows: int
    _intraday_covered_rows: int
    _intraday_latest_source_time: str
    _intraday_sources: tuple[str, ...]
    _intraday_data_versions: tuple[str, ...]
    _tushare_reference_fields: dict[str, dict[str, float]]
    _tushare_reference_versions: dict[str, str]
    _tushare_reference_version_order: dict[str, tuple[datetime, datetime, str]]

    def _ensure_before_deadline(self, deadline: datetime | None) -> None:
        raise NotImplementedError

    def _data_cache_identity(
        self,
        dataset: str,
        source: str,
        subject_key: str,
        request: Mapping[str, object],
        observed_at: datetime,
    ) -> CacheIdentity:
        raise NotImplementedError

    def _trim_history_fallback_locked(self, requested: set[str]) -> None:
        raise NotImplementedError

    def _history_summaries(
        self,
        histories: Mapping[str, tuple[DailyBar, ...]],
        observed_at: datetime,
    ) -> Mapping[str, HistoryProfile]:
        raise NotImplementedError

    def _run_source_task(
        self,
        source: str,
        identity: str,
        observed_at: datetime,
        function: Callable[..., Any],
        /,
        *args: object,
        **kwargs: object,
    ) -> Any:
        raise NotImplementedError

    def _load_histories(
        self,
        codes: tuple[str, ...],
        *,
        force: bool = False,
        deadline: datetime | None = None,
    ) -> Mapping[str, object]:
        raise NotImplementedError
