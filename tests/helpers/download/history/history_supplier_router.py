from __future__ import annotations
from dataclasses import replace
from datetime import date
from tests.helpers.shared.reference_fixtures import PreparedFixture
from tests.helpers.download.history.infra.history_sync import NOW, FakeSupplier, _download
from trader.download.domain.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyFact,
    BaoStockSecurity,
)
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_supplier_router import HistorySupplierRouter


class Baseline(FakeSupplier):
    def fetch_code(self, security, dates):
        # These fixtures exercise Tencent-only recovery. Serial second-source
        # success is covered by test_price_repair_pipeline.
        raise RuntimeError("fixture_baostock_unavailable")

    def fetch_raw_code(self, security, dates):
        self.calls.append(("raw", dates))
        value = _download(security.code, dates)
        cells = tuple(replace(cell, qfq=None, status="qfq_missing") for cell in value.batch.cells)
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, cells),
            tuple(BaoStockDailyFact(security.code, day, True) for day in dates),
        )


class Prices:
    def __init__(self, *, missing=False, conflict=False, shift=0.5):
        self.calls = []
        self.missing = missing
        self.conflict = conflict
        self.shift = shift
        self.dates = None

    def load_history_context(self, as_of, sessions, universe, *, calendar):
        dates = calendar.open_dates
        return replace(FakeSupplier(dates).load_context(as_of, sessions, universe=universe), industry_intervals=())

    def fetch_window(self, security, dates, **kwargs):
        self.calls.append(dates)
        cells = []
        for value in _download(security.code, dates, qfq_shift=self.shift).batch.cells:
            cell = ProjectHistoryCell(value)
            raw = replace(cell.unadjusted, preclose=None, pct_change=None, turnover=None)
            if self.conflict:
                raw = replace(raw, close_price=raw.close_price + 1)
            cells.append(
                PublishedHistoryCell(
                    cell.code,
                    cell.trade_date,
                    "qfq_missing" if self.missing else "complete",
                    raw,
                    None if self.missing else cell.qfq,
                )
            )
        return PublishedHistoryWindow(security.code, tuple(cells))


class StSource:
    def fetch(self, universe, as_of):
        return tuple(HistoryStEvidence(item.code, as_of, "clear") for item in universe)


def _current_universe(*codes: str) -> tuple[BaoStockSecurity, ...]:
    return tuple(
        BaoStockSecurity(
            code,
            code,
            "chinext" if code.startswith(("300", "301", "302")) else "main",
            date(2000, 1, 1),
            None,
            "exchange_security_master",
        )
        for code in codes
    )


def _router(
    baseline: Baseline,
    prices: Prices,
    universe: tuple[BaoStockSecurity, ...] | None = None,
) -> HistorySupplierRouter:
    selected = universe or _current_universe("600001", "600002")
    prices.dates = baseline.dates
    return HistorySupplierRouter(baseline, prices, _prepared(selected, baseline.dates))


def _at(day):
    return NOW.replace(year=day.year, month=day.month, day=day.day)


def _prepared(universe, dates, checked=None):
    checked = checked or _at(dates[-1])
    return PreparedFixture(checked, universe, open_dates=tuple(sorted(set((*dates, checked.date())))))
