"""Use Tencent for small history rereads while retaining the archive baseline supplier."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Protocol

import requests

from trader.download.domain.baostock_daily import BaoStockCodeDownload, BaoStockDailyFact, BaoStockSecurity
from trader.download.domain.history_sync import HistorySupplierContext, HistorySyncSupplier
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier


class HistoryFactsSupplier(HistorySyncSupplier, Protocol):
    def fetch_daily_facts(
        self, security: BaoStockSecurity, dates: tuple[date, ...]
    ) -> tuple[BaoStockDailyFact, ...]: ...


class TencentHistoryTailSupplier:
    """Delegate full windows to the baseline and bounded tails to Tencent."""

    def __init__(
        self,
        baseline: HistoryFactsSupplier,
        *,
        timeout_seconds: float = 15.0,
        workers: int = 8,
        session_factory: Callable[[], requests.Session] = requests.Session,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        self._baseline = baseline
        self._timeout_seconds = timeout_seconds
        self._workers = workers
        self._session_factory = session_factory
        self._cancel_requested = cancel_requested
        self._tail: TencentQfqSupplier | None = None

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        context = self._baseline.load_context(as_of, sessions)
        self._tail = TencentQfqSupplier(
            context,
            workers=self._workers,
            timeout_seconds=self._timeout_seconds,
            session_factory=self._session_factory,
            cancel_requested=self._cancel_requested,
        )
        return context

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        if len(dates) <= 640 and self._tail is not None:
            download = self._tail.fetch_code(security, dates)
            facts = self._fetch_facts(security, dates)
            return BaoStockCodeDownload(download.batch, facts)
        return self._baseline.fetch_code(security, dates)

    def _fetch_facts(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> tuple[BaoStockDailyFact, ...]:
        return self._baseline.fetch_daily_facts(security, dates)


__all__ = ["HistoryFactsSupplier", "TencentHistoryTailSupplier"]
