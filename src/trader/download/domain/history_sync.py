"""Typed contracts for zero-argument historical synchronization."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, get_args

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeDownload,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.published_history import PublishedHistoryWindow

HistorySyncProgressStage = Literal[
    "initializing",
    "loading_context",
    "supplier_login",
    "supplier_calendar",
    "supplier_universe",
    "supplier_industry",
    "supplier_daily_raw",
    "supplier_daily_qfq",
    "supplier_routing",
    "tencent_history",
    "history_gap_inventory",
    "baostock_gap_fill",
    "preparing_partitions",
    "downloading_codes",
    "sealing_partitions",
    "publishing_snapshot",
]
HistorySyncProgressState = Literal["started", "waiting", "retrying", "completed", "failed", "cancelled"]
_PROGRESS_ITEM = re.compile(r"[0-9A-Za-z_.:-]{1,64}")
_PROGRESS_STAGES = frozenset(get_args(HistorySyncProgressStage))
_PROGRESS_STATES = frozenset(get_args(HistorySyncProgressState))
BAOSTOCK_MIN_QUERY_INTERVAL_SECONDS = 1.5
BAOSTOCK_PREPARATION_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class HistorySyncConfiguration:
    archive_root: Path = Path("data/history/baostock")
    sessions: int = 2000
    reread_sessions: int = 5
    download_batch_size: int = 32
    history_workers: int = 8
    minimum_free_bytes: int = 1 * 1024**3
    supplier_timeout_seconds: float = 45.0
    supplier_retries: int = 2
    query_interval_seconds: float = BAOSTOCK_MIN_QUERY_INTERVAL_SECONDS
    cancellation_grace_seconds: float = 10.0
    progress_heartbeat_seconds: float = 5.0
    training_root: Path = Path("data/train")

    @classmethod
    def for_repository(cls, repository_root: Path) -> HistorySyncConfiguration:
        root = repository_root.resolve()
        return cls(
            archive_root=root / "data" / "history" / "baostock",
            training_root=root / "data" / "train",
        )

    def __post_init__(self) -> None:
        if (
            not 1 <= self.sessions <= 2000
            or not 1 <= self.reread_sessions <= self.sessions
            or not 1 <= self.download_batch_size <= 256
            or not 1 <= self.history_workers <= 12
            or self.minimum_free_bytes < 0
            or self.supplier_timeout_seconds <= 0
            or not 0 <= self.supplier_retries <= 2
            or self.query_interval_seconds < BAOSTOCK_MIN_QUERY_INTERVAL_SECONDS
            or not 0 < self.cancellation_grace_seconds <= 10.0
            or not 0 < self.progress_heartbeat_seconds <= self.supplier_timeout_seconds
        ):
            raise ValueError("history synchronization configuration is invalid")


@dataclass(frozen=True)
class HistorySupplierContext:
    calendar: BaoStockCalendar
    universe: tuple[BaoStockSecurity, ...]
    source_versions: BaoStockSourceVersions
    industry_intervals: tuple[BaoStockIndustryInterval, ...] = ()

    def __post_init__(self) -> None:
        universe = tuple(sorted(self.universe, key=lambda item: item.code))
        intervals = tuple(sorted(self.industry_intervals, key=lambda item: (item.code, item.effective_from)))
        codes = {item.code for item in universe}
        if not universe or len(codes) != len(universe) or any(item.code not in codes for item in intervals):
            raise ValueError("history supplier context is invalid")
        object.__setattr__(self, "universe", universe)
        object.__setattr__(self, "industry_intervals", intervals)


@dataclass(frozen=True)
class HistoryGapSummary:
    metadata_cells: int
    price_pair_cells: int
    failed_codes: int

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (
                self.metadata_cells,
                self.price_pair_cells,
                self.failed_codes,
            )
        ):
            raise ValueError("history gap summary is invalid")


@dataclass(frozen=True)
class HistorySyncProgress:
    stage: HistorySyncProgressStage
    state: HistorySyncProgressState
    completed_units: int
    total_units: int
    current_item: str | None = None
    attempt: int = 1
    max_attempts: int = 1
    call_elapsed_seconds: float = 0.0
    supplier_source: Literal["baostock", "tencent"] | None = None
    requested_sessions: int | None = None
    gap_summary: HistoryGapSummary | None = None

    def __post_init__(self) -> None:
        if (
            self.stage not in _PROGRESS_STAGES
            or self.state not in _PROGRESS_STATES
            or type(self.completed_units) is not int
            or type(self.total_units) is not int
            or not 0 <= self.completed_units <= self.total_units
            or type(self.attempt) is not int
            or type(self.max_attempts) is not int
            or not 1 <= self.attempt <= self.max_attempts
            or not math.isfinite(self.call_elapsed_seconds)
            or self.call_elapsed_seconds < 0.0
            or (self.current_item is not None and _PROGRESS_ITEM.fullmatch(self.current_item) is None)
            or self.supplier_source not in (None, "baostock", "tencent")
            or (self.supplier_source is None) != (self.requested_sessions is None)
            or (
                self.requested_sessions is not None
                and (type(self.requested_sessions) is not int or not 1 <= self.requested_sessions <= 2000)
            )
        ):
            raise ValueError("history synchronization progress is invalid")


class HistorySyncProgressPort(Protocol):
    def publish(self, progress: HistorySyncProgress) -> None: ...


class HistoryTwoStageSupplier(Protocol):
    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow: ...

    def fetch_baostock_raw(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload: ...

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload: ...


__all__ = [
    "HistorySupplierContext",
    "HistorySyncConfiguration",
    "HistorySyncProgress",
    "HistorySyncProgressPort",
    "HistorySyncProgressStage",
    "HistoryGapSummary",
    "HistoryTwoStageSupplier",
]
