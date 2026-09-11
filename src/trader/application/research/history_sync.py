"""Typed contracts for zero-argument historical synchronization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeDownload,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)


@dataclass(frozen=True)
class HistorySyncConfiguration:
    archive_root: Path = Path("data/history/baostock")
    sessions: int = 2000
    reread_sessions: int = 5
    minimum_free_bytes: int = 25 * 1024**3
    supplier_timeout_seconds: float = 45.0
    supplier_retries: int = 2
    query_interval_seconds: float = 2.0
    cancellation_grace_seconds: float = 10.0
    training_root: Path = Path("data/train")

    def __post_init__(self) -> None:
        if (
            not 1 <= self.sessions <= 2000
            or not 1 <= self.reread_sessions <= self.sessions
            or self.minimum_free_bytes < 0
            or self.supplier_timeout_seconds <= 0
            or not 0 <= self.supplier_retries <= 2
            or self.query_interval_seconds < 2.0
            or not 0 < self.cancellation_grace_seconds <= 10.0
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


class HistorySyncSupplier(Protocol):
    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext: ...

    def fetch_code(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
    ) -> BaoStockCodeDownload: ...


__all__ = ["HistorySupplierContext", "HistorySyncConfiguration", "HistorySyncSupplier"]
