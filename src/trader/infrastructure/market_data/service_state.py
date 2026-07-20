"""Typed shared state contract for MarketFeatureService mixins."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from trader.application.workers import BoundedExecutor
from trader.infrastructure.market_data.akshare import AkshareResearchClient
from trader.infrastructure.market_data.eastmoney import EastmoneyClient
from trader.infrastructure.market_data.service_models import _HistoryEntry, _IntradayEntry, _ResearchEntry
from trader.infrastructure.persistence.runtime_json import RuntimeJsonWriter


class MarketServiceState:
    _history_client: EastmoneyClient
    _research_client: AkshareResearchClient | None
    _intraday_client: EastmoneyClient | None
    _worker_pool: BoundedExecutor | None
    _json_writer: RuntimeJsonWriter | None
    _research_cache_dir: Path | None
    _monotonic: Callable[[], float]
    _wall_clock: Callable[[], datetime]
    _lock: Any
    _history: dict[str, _HistoryEntry]
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
    _history_error_count: int
    _history_out_of_order_count: int
    _research_success_count: int
    _research_error_count: int
    _research_last_error: str
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

    def _ensure_before_deadline(self, deadline: datetime | None) -> None:
        raise NotImplementedError
