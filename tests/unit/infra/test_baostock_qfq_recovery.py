from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.download.domain.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockSecurity,
)
from trader.download.infra.baostock_qfq_recovery import BaoStockQfqRecovery

SECURITY = BaoStockSecurity("600001", "fixture", "main", date(2020, 1, 1), None, "fixture")
DAYS = tuple(date(2026, 10, 7) + timedelta(days=offset) for offset in range(3))


def _cell(day: date, *, suspended: bool = False) -> BaoStockDailyCell:
    raw = BaoStockDailySide("600001", day, "unadjusted", 10, 11, 9, 10, 1000, 10000, 9.9, 1, 2, "trading")
    if suspended:
        raw = replace(
            raw,
            open_price=None,
            high_price=None,
            low_price=None,
            close_price=None,
            volume=None,
            amount=None,
            preclose=None,
            pct_change=None,
            turnover=None,
            trading_status="suspended",
        )
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    return BaoStockDailyCell("600001", day, "supplier_marked_suspended" if suspended else "complete", raw, qfq)


class _BaoStock:
    def __init__(self, batch: BaoStockCodeBatch) -> None:
        self.batch = batch
        self.calls: list[tuple[date, ...]] = []

    def fetch_code(self, security, dates):
        self.calls.append(dates)
        facts = tuple(BaoStockDailyFact(security.code, cell.trade_date, False) for cell in self.batch.cells)
        return BaoStockCodeDownload(self.batch, facts)


def test_recovers_full_bounded_pair_including_explicit_suspension() -> None:
    supplier = _BaoStock(BaoStockCodeBatch(SECURITY.code, tuple(_cell(day, suspended=day == DAYS[1]) for day in DAYS)))
    recovery = BaoStockQfqRecovery(supplier, "baostock:fixture")
    result = recovery.fetch_window(SECURITY, DAYS)
    assert supplier.calls == [DAYS]
    assert recovery.source_identity == "baostock:fixture"
    assert len(result.cells) == 3
    assert all(cell.unadjusted is not None and cell.qfq is not None for cell in result.cells)
    assert result.cells[1].unadjusted.trading_status == "suspended"
    assert result.cells[1].qfq.close_price is None


@pytest.mark.parametrize(
    "field,value",
    (
        ("duplicate_rows", 1),
        ("null_rows", 1),
        ("out_of_window_rows", 1),
        ("future_rows", 1),
        ("failure_reasons", ("supplier_failed",)),
    ),
)
def test_rejects_anomalous_batch(field, value) -> None:
    batch = replace(BaoStockCodeBatch(SECURITY.code, tuple(_cell(day) for day in DAYS)), **{field: value})
    with pytest.raises(RuntimeError, match="qfq_recovery_batch_invalid"):
        BaoStockQfqRecovery(_BaoStock(batch), "fixture").fetch_window(SECURITY, DAYS)


@pytest.mark.parametrize("days", ((), tuple(date(2025, 1, 1) + timedelta(days=offset) for offset in range(252))))
def test_rejects_unbounded_or_empty_request_without_call(days) -> None:
    supplier = _BaoStock(BaoStockCodeBatch(SECURITY.code, ()))
    with pytest.raises(ValueError, match="out_of_bounds"):
        BaoStockQfqRecovery(supplier, "fixture").fetch_window(SECURITY, days)
    assert supplier.calls == []
