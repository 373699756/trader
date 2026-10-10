from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.unit.infra.research.test_history_archive_sync import NOW, FakeSupplier, _download
from trader.download.domain.baostock_daily import BaoStockCodeBatch, BaoStockCodeDownload, BaoStockDailyFact
from trader.download.domain.history_sync import HistoryGapSummary, HistorySyncConfiguration, HistorySyncProgress
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow, project_history_cell
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.history_archive_reader import SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_repository import SQLiteHistoryControlRepository
from trader.download.infra.history_supplier_router import HistorySupplierRouter


class Baseline(FakeSupplier):
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

    def fetch_window(self, security, dates):
        self.calls.append(dates)
        cells = []
        for value in _download(security.code, dates, qfq_shift=self.shift).batch.cells:
            cell = project_history_cell(value)
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


@pytest.mark.parametrize("count", (5, 639, 640, 641, 2000))
def test_tencent_first_fetches_every_window_in_bounded_segments(count):
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(count))
    baseline, prices = Baseline(dates), Prices()
    router = HistorySupplierRouter(baseline, prices)
    context = router.load_context(dates[-1], count)
    security = context.universe[0]
    result = router.fetch_tencent_window(security, dates)
    assert tuple(cell.trade_date for cell in result.cells) == dates
    assert prices.calls
    assert all(1 <= len(segment) <= 640 for segment in prices.calls)
    assert sum(len(segment) for segment in prices.calls) >= count
    metadata = router.fetch_baostock_raw(security, dates)
    assert baseline.calls == [("raw", dates)]
    assert metadata.daily_facts[0].is_st is True
    assert metadata.batch.cells[0].unadjusted.preclose == 1.0
    assert metadata.batch.cells[0].unadjusted.turnover == 0.01
    assert context.source_versions != baseline.load_context(dates[-1], count).source_versions


@pytest.mark.parametrize("invalid", ("missing", "conflict", "qfq_basis"))
def test_invalid_tencent_tail_preserves_active_snapshot_and_checkpoint(tmp_path: Path, invalid):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert (
        run_history_sync(config, HistorySupplierRouter(Baseline(dates), Prices()), clock=lambda: NOW).state
        == "completed"
    )
    before = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline = Baseline(new_dates)
    result = run_history_sync(
        config,
        HistorySupplierRouter(baseline, Prices(shift=1.0) if invalid == "qfq_basis" else Prices(**{invalid: True})),
        clock=lambda: NOW,
    )
    state = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state()
    assert result.state == "failed"
    assert state.active_snapshot == before
    assert state.checkpoints[-1].completed_units == 2
    assert any(code == "raw" for code, _dates in baseline.calls)
    rows = SQLiteHistoryArchiveReader(tmp_path).read_day(dates[-1], before)
    assert rows[0].is_st is True
    assert rows[0].cell.qfq.close_price == 9.5


def test_bootstrap_wires_routing_to_real_snapshot_publication(tmp_path, monkeypatch):
    from trader.bootstrap import execute_history_download

    dates = (date(2026, 9, 8), date(2026, 9, 9))
    baseline = Baseline(dates)

    class Session(Baseline):
        def __enter__(self):
            return baseline

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr("trader.bootstrap.BaoStockHistorySupplier", lambda *_args, **_kwargs: Session(dates))
    monkeypatch.setattr("trader.bootstrap.TencentQfqSupplier", lambda _dependencies: Prices())
    result = execute_history_download(
        HistorySyncConfiguration(tmp_path, sessions=2, reread_sessions=2, minimum_free_bytes=0),
        clock=lambda: NOW,
    )
    assert result.state == "completed"
    assert baseline.calls == [("raw", dates), ("raw", dates)]
    snapshot = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert snapshot is not None
    rows = SQLiteHistoryArchiveReader(tmp_path).read_day(dates[-1], snapshot)
    assert rows[0].is_st is True
    assert rows[0].cell.qfq.close_price == 9.5


def test_valid_increment_requests_only_tail_and_publishes_new_day(tmp_path):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert (
        run_history_sync(config, HistorySupplierRouter(Baseline(dates), Prices()), clock=lambda: NOW).state
        == "completed"
    )
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline, prices = Baseline(new_dates), Prices()
    result = run_history_sync(config, HistorySupplierRouter(baseline, prices), clock=lambda: NOW)
    assert result.state == "completed"
    assert prices.calls == [new_dates, new_dates]
    assert baseline.calls == [("raw", new_dates), ("raw", new_dates)]
    snapshot = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert snapshot.sequence == 2
    assert len(SQLiteHistoryArchiveReader(tmp_path).read_day(new_dates[-1], snapshot)) == 2


def test_new_stock_short_history_and_existing_stock_both_use_tencent(tmp_path):
    from trader.download.infra.history_archive_sync import _CodeDownloadContext, _requested_dates

    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    baseline, prices = Baseline(dates), Prices()
    router = HistorySupplierRouter(baseline, prices)
    context = router.load_context(dates[-1], 2000)
    new_stock = replace(context.universe[1], listed_on=dates[-639])
    context = replace(context, universe=(context.universe[0], new_stock))
    request = _CodeDownloadContext(HistorySyncConfiguration(tmp_path), context, None, frozenset())
    assert _requested_dates(request, context.universe[0]) == dates
    assert _requested_dates(request, new_stock) == dates[-639:]
    assert router.fetch_tencent_window(context.universe[0], dates).cells
    assert router.fetch_tencent_window(new_stock, dates[-639:]).cells
    assert tuple(map(len, prices.calls)) == (640, 640, 640, 95, 639)


def test_gap_longer_than_reread_window_includes_published_qfq_anchor(tmp_path):
    dates = tuple(date(2026, 8, 25) + timedelta(days=i) for i in range(10))
    config = HistorySyncConfiguration(tmp_path, sessions=10, reread_sessions=2, minimum_free_bytes=0)
    first = run_history_sync(config, HistorySupplierRouter(Baseline(dates), Prices()), clock=lambda: NOW)
    assert first.state == "completed"
    shifted = tuple(day + timedelta(days=6) for day in dates)
    prices = Prices()
    result = run_history_sync(config, HistorySupplierRouter(Baseline(shifted), prices), clock=lambda: NOW)
    assert result.state == "completed"
    assert prices.calls == [shifted[-8:], shifted[-8:]]


def test_changed_price_contract_does_not_resume_legacy_short_batches():
    from trader.download.domain.history_control import HistorySyncCheckpoint
    from trader.download.infra.history_archive_sync import _matching_checkpoints

    previous = HistorySyncCheckpoint("sync-20260909-old", 1, "running", NOW, 1, 2, None)
    assert _matching_checkpoints((previous,), "sync-20260909-new") == ()
    assert _matching_checkpoints((previous,), previous.sync_identity) == (previous,)


def test_only_explicit_baseline_suspension_can_qualify_missing_tencent_prices():
    from trader.download.domain.history_price_qualification import combine_history_sources

    day = date(2026, 9, 9)
    baseline = Baseline((day,))
    security = baseline.load_context(day, 1).universe[0]
    evidence = baseline.fetch_raw_code(security, (day,))
    raw = replace(evidence.batch.cells[0].unadjusted, trading_status="suspended")
    evidence = replace(
        evidence,
        batch=replace(
            evidence.batch,
            cells=(
                replace(
                    evidence.batch.cells[0],
                    unadjusted=raw,
                ),
            ),
        ),
    )
    missing = PublishedHistoryWindow(
        security.code,
        (
            PublishedHistoryCell(
                security.code,
                day,
                "unknown_missing",
                None,
                None,
            ),
        ),
    )
    value = combine_history_sources(security, (day,), missing, evidence, None)
    assert value.batch.cells[0].status == "supplier_marked_suspended"
    assert value.daily_facts[0].is_st is True
    with pytest.raises(RuntimeError, match="trading_status_conflict"):
        combine_history_sources(security, (day,), Prices().fetch_window(security, (day,)), evidence, None)


def test_completed_stock_reports_real_route_and_requested_sessions(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)
    progress.publish(
        HistorySyncProgress(
            "supplier_routing",
            "started",
            0,
            1,
            "600001",
            supplier_source="tencent",
            requested_sessions=5,
        )
    )
    progress.publish(HistorySyncProgress("downloading_codes", "completed", 1, 2, "600001"))
    assert "来源 Tencent | 请求 5 日" in capsys.readouterr().err


def test_gap_inventory_reports_missing_field_and_price_counts(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)
    progress.publish(
        HistorySyncProgress(
            "history_gap_inventory",
            "completed",
            2,
            2,
            gap_summary=HistoryGapSummary(6, 1, 0),
        )
    )
    assert "统计历史缺口 | 完成 | 元数据待补 6 股日 | 价格对待补 1 股日 | Tencent失败 0 股" in capsys.readouterr().err
