from __future__ import annotations

import json
from datetime import date

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.infra.tencent_history_tail import TencentHistoryTailSupplier
from trader.download.infra.tencent_qfq_supplier import TencentQfqSupplier, context_from_manifest


class _Session:
    def __enter__(self) -> _Session:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, _url: str, **kwargs: object) -> object:
        params = kwargs.get("params")
        mode = "qfq" if isinstance(params, dict) and params.get("param", "").endswith(",qfq") else "raw"
        return _Response(mode)


def _session_factory() -> _Session:
    return _Session()


class _Response:
    def __init__(self, mode: str) -> None:
        rows = [["2026-10-09", "10", "10.5", "11", "9.5", "100", "", "1.2", "20"]]
        payload = {"data": {"sh600001": {"qfqday" if mode == "qfq" else "day": rows}}}
        self.text = "v={" + json.dumps(payload)[1:]

    def raise_for_status(self) -> None:
        return None


def _context() -> HistorySupplierContext:
    return HistorySupplierContext(
        BaoStockCalendar((date(2026, 10, 9),)),
        (BaoStockSecurity("600001", "fixture", "main", date(2020, 1, 1), None, "fixture"),),
        BaoStockSourceVersions("fixture", "3.14", ()),
    )


def test_context_from_manifest_preserves_bounded_calendar_and_codes() -> None:
    context = context_from_manifest((date(2026, 10, 9),), ("600001",))
    assert context.calendar.open_dates == (date(2026, 10, 9),)
    assert context.universe[0].code == "600001"


def test_tencent_qfq_supplier_pairs_raw_and_qfq_rows() -> None:
    supplier = TencentQfqSupplier(_context(), session_factory=_session_factory)
    result = supplier.fetch_code(_context().universe[0], (date(2026, 10, 9),))
    assert result.batch.cells[0].status == "complete"
    assert result.batch.cells[0].qfq is not None
    assert result.batch.cells[0].unadjusted is not None


def test_history_tail_uses_baseline_for_large_windows() -> None:
    class Baseline:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
            return _context()

        def fetch_code(self, security, dates):
            self.calls.append(len(dates))
            raise RuntimeError("baseline_called")

    baseline = Baseline()
    supplier = TencentHistoryTailSupplier(baseline)
    supplier.load_context(date(2026, 10, 9), 2000)
    try:
        supplier.fetch_code(
            baseline.load_context(date(2026, 10, 9), 1).universe[0], tuple(date(2020, 1, 1) for _ in range(641))
        )
    except RuntimeError as exc:
        assert str(exc) == "baseline_called"
    assert baseline.calls == [641]
