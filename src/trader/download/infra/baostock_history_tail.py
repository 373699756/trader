"""Same-source candidate tails through the existing bounded BaoStock worker."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from trader.download.domain.baostock_daily import BaoStockDailyJoinRequest, BaoStockDailySide, join_baostock_daily_sides
from trader.download.domain.history_tail import HistoryTailRequest, HistoryTailWindow
from trader.download.infra.baostock_gap_supplier import BaoStockGapRequest, BaoStockGapResult, BaoStockGapWorkerOptions
from trader.download.infra.history_control_repository import HistoryMaintenanceLock
from trader.download.infra.history_revision_codec import decode_history_side


class BaoStockTailWorker(Protocol):
    def __call__(
        self,
        requests: tuple[BaoStockGapRequest, ...],
        *,
        options: BaoStockGapWorkerOptions,
        cancel_requested: Callable[[], bool],
    ) -> BaoStockGapResult: ...


class BaoStockHistoryTailSupplier:
    def __init__(
        self,
        archive_root: Path,
        worker: BaoStockTailWorker,
        *,
        cancel_requested: Callable[[], bool],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._archive_root = archive_root
        self._worker = worker
        self._cancel_requested = cancel_requested
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._next_start = 0.0

    def fetch(self, request: HistoryTailRequest, *, deadline: float) -> HistoryTailWindow:
        # A separate process cannot inherit the SDK's previous-call clock.
        # Keep at least two seconds between workers as well as within them.
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("history_tail_supplier_busy")
        try:
            while self._monotonic() < self._next_start:
                self._check_budget(deadline)
                time.sleep(max(0.0, min(0.05, self._next_start - self._monotonic())))
            self._check_budget(deadline)
            with HistoryMaintenanceLock(self._archive_root / ".maintenance.lock"):
                try:
                    result = self._worker(
                        tuple(
                            BaoStockGapRequest(request.code, family, request.dates)
                            for family in ("daily_raw", "daily_qfq")
                        ),
                        options=BaoStockGapWorkerOptions(0, min(8.0, deadline - self._monotonic()), 1.0),
                        cancel_requested=lambda: self._cancel_requested() or self._monotonic() >= deadline,
                    )
                finally:
                    self._next_start = self._monotonic() + 2.0
            self._check_budget(deadline)
            raw: list[BaoStockDailySide] = []
            qfq: list[BaoStockDailySide] = []
            for record in result.records:
                if record.code != request.code or record.trade_date not in request.dates:
                    raise ValueError("history_tail_response_invalid")
                side = decode_history_side(json.loads(record.payload_json), record.family)
                if side is None:
                    raise ValueError("history_tail_response_invalid")
                (raw if record.family == "daily_raw" else qfq).append(side)
            batch = join_baostock_daily_sides(
                BaoStockDailyJoinRequest(request.code, request.dates, request.dates[-1]), tuple(raw), tuple(qfq)
            )
            if batch.duplicate_rows or batch.future_rows or batch.out_of_window_rows:
                raise ValueError("history_tail_response_invalid")
            return HistoryTailWindow(request.code, "baostock", batch.cells)
        finally:
            self._lock.release()

    def _check_budget(self, deadline: float) -> None:
        if self._cancel_requested():
            raise RuntimeError("history_tail_cancelled")
        if self._monotonic() >= deadline:
            raise TimeoutError("history_tail_deadline")
