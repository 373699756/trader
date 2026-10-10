from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.unit.infra.research.test_history_archive_sync import NOW, FakeSupplier, _download
from trader.download.domain.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyFact,
    BaoStockSecurity,
)
from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistoryGapSummary, HistorySyncConfiguration, HistorySyncProgress
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistoryWindow, project_history_cell
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.history_archive_reader import SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_repository import SQLiteHistoryControlRepository
from trader.download.infra.history_supplier_router import HistorySupplierRouter
from trader.download.infra.published_history_archive import SQLitePublishedHistoryArchive
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputArchive
from trader.training.infra.research.historical_industry_archive import audit_archived_historical_industry_facts


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
    return HistorySupplierRouter(baseline, prices, lambda: selected, StSource())


def test_history_context_uses_the_current_exchange_universe() -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = _current_universe("600001", "302132")
    context = _router(Baseline(dates), Prices(), universe).load_context(dates[-1], len(dates))

    assert context.universe == tuple(sorted(universe, key=lambda item: item.code))
    assert context.universe[0].board == "chinext"
    assert HISTORY_UNIVERSE_CONTRACT in context.source_versions.dependency_versions


@pytest.mark.parametrize("count", (5, 639, 640, 641, 2000))
def test_tencent_first_fetches_every_window_in_bounded_segments(count):
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(count))
    baseline, prices = Baseline(dates), Prices()
    router = _router(baseline, prices)
    context = router.load_context(dates[-1], count)
    security = context.universe[0]
    result = router.fetch_tencent_window(security, dates)
    assert tuple(cell.trade_date for cell in result.cells) == dates
    assert prices.calls
    assert all(1 <= len(segment) <= 640 for segment in prices.calls)
    assert sum(len(segment) for segment in prices.calls) >= count
    assert baseline.calls == []
    assert context.source_versions != baseline.load_context(dates[-1], count).source_versions


@pytest.mark.parametrize("invalid", ("missing", "qfq_basis"))
def test_invalid_tencent_tail_preserves_active_snapshot_and_checkpoint(tmp_path: Path, invalid):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(config, _router(Baseline(dates), Prices()), clock=lambda: NOW).state == "completed"
    before = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline = Baseline(new_dates)
    result = run_history_sync(
        config,
        _router(baseline, Prices(shift=1.0) if invalid == "qfq_basis" else Prices(**{invalid: True})),
        clock=lambda: NOW,
    )
    state = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state()
    assert result.state == "failed"
    assert state.active_snapshot == before
    assert state.checkpoints[-1].completed_units == 2
    assert not any(code == "raw" for code, _dates in baseline.calls)
    rows = SQLiteHistoryArchiveReader(tmp_path).read_day(dates[-1], before)
    assert rows[0].is_st is False
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
    monkeypatch.setattr("trader.bootstrap.HistoryStNameSource", lambda *_args, **_kwargs: StSource())
    monkeypatch.setattr(
        "trader.bootstrap.load_current_a_share_universe",
        lambda *_args: _current_universe("600001", "600002"),
    )
    result = execute_history_download(
        HistorySyncConfiguration(tmp_path, sessions=2, reread_sessions=2, minimum_free_bytes=0),
        clock=lambda: NOW,
    )
    assert result.state == "completed"
    assert baseline.calls == []
    snapshot = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert snapshot is not None
    rows = SQLiteHistoryArchiveReader(tmp_path).read_day(dates[-1], snapshot)
    assert rows[0].is_st is False
    assert rows[0].cell.qfq.close_price == 9.5


def test_valid_increment_requests_only_tail_and_publishes_new_day(tmp_path):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(config, _router(Baseline(dates), Prices()), clock=lambda: NOW).state == "completed"
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline, prices = Baseline(new_dates), Prices()
    result = run_history_sync(config, _router(baseline, prices), clock=lambda: NOW)
    assert result.state == "completed"
    assert prices.calls == [new_dates, new_dates]
    assert baseline.calls == []
    snapshot = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert snapshot.sequence == 2
    assert len(SQLiteHistoryArchiveReader(tmp_path).read_day(new_dates[-1], snapshot)) == 2


def test_exchange_universe_shrink_publishes_only_current_codes(tmp_path) -> None:
    dates = tuple(date(2026, 8, 10) + timedelta(days=i) for i in range(61))
    config = HistorySyncConfiguration(tmp_path / "baostock", sessions=61, reread_sessions=2, minimum_free_bytes=0)
    full = _current_universe("600001", "600002")
    assert (
        run_history_sync(config, _router(Baseline(dates, industry="银行"), Prices(), full), clock=lambda: NOW).state
        == "completed"
    )

    current = _current_universe("600001")
    result = run_history_sync(config, _router(Baseline(dates, industry="银行"), Prices(), current), clock=lambda: NOW)

    assert result.state == "completed"
    archive = SQLitePublishedHistoryArchive(config.archive_root)
    manifest = archive.manifest()
    assert manifest is not None
    assert manifest.universe_codes == ("600001",)
    assert tuple(window.code for window in archive.iter_windows(manifest, sessions=61)) == ("600001",)
    assert tuple(window.code for window in archive.read_windows(manifest, ("600001", "600002"), sessions=61)) == (
        "600001",
    )
    snapshot = SQLiteHistoryControlRepository(config.archive_root / "control.sqlite3").load_state().active_snapshot
    assert snapshot is not None
    assert tuple(row.code for row in SQLiteHistoryArchiveReader(config.archive_root).read_day(dates[-1], snapshot)) == (
        "600001",
        "600002",
    )
    training = SQLiteHistoryTrainingInputArchive.open(config.archive_root)
    assert training.snapshot.training_codes == ("600001",)
    assert training.count_training_rows(frozenset(dates)) == len(dates)
    assert tuple(window.code for window in training.iter_training_windows(frozenset(dates))) == ("600001",)
    industry_report = audit_archived_historical_industry_facts(config.archive_root)
    assert industry_report.sources[0].sampled_codes == 1
    assert len(industry_report.merged_fact_hashes) == 1


def test_unmarked_supplier_universe_shrink_still_fails_closed(tmp_path) -> None:
    dates = tuple(date(2026, 10, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(config, Baseline(dates), clock=lambda: NOW).state == "completed"

    class Regressed(Baseline):
        def load_context(self, as_of, sessions, *, universe=None):
            context = super().load_context(as_of, sessions, universe=universe)
            return replace(context, universe=(context.universe[0],))

    result = run_history_sync(config, Regressed(dates), clock=lambda: NOW)

    assert result.state == "failed"
    assert result.reason == "supplier_universe_regressed"


@pytest.mark.parametrize("failure", (TimeoutError, ValueError))
def test_unavailable_official_universe_preserves_active_snapshot(tmp_path, failure) -> None:
    dates = tuple(date(2026, 10, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(config, _router(Baseline(dates), Prices()), clock=lambda: NOW).state == "completed"
    control = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    before = control.load_state().active_snapshot

    def unavailable():
        raise failure("fixture official universe unavailable")

    class NoContext(Baseline):
        def load_context(self, *_args, **_kwargs):
            raise AssertionError("BaoStock must not replace an unavailable official universe")

    baseline, prices = NoContext(dates), Prices()
    result = run_history_sync(
        config, HistorySupplierRouter(baseline, prices, unavailable, StSource()), clock=lambda: NOW
    )

    assert result.state == "failed"
    assert control.load_state().active_snapshot == before
    assert baseline.calls == []
    assert prices.calls == []


def test_new_stock_short_history_and_existing_stock_both_use_tencent(tmp_path):
    from trader.download.infra.history_archive_sync import _CodeDownloadContext, _requested_dates

    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    baseline, prices = Baseline(dates), Prices()
    router = _router(baseline, prices)
    context = router.load_context(dates[-1], 2000)
    new_stock = replace(context.universe[1], listed_on=dates[-639])
    context = replace(context, universe=(context.universe[0], new_stock))
    request = _CodeDownloadContext(HistorySyncConfiguration(tmp_path), context, None, frozenset(), {})
    assert _requested_dates(request, context.universe[0]) == dates
    assert _requested_dates(request, new_stock) == dates[-639:]
    assert router.fetch_tencent_window(context.universe[0], dates).cells
    assert router.fetch_tencent_window(new_stock, dates[-639:]).cells
    assert tuple(map(len, prices.calls)) == (640, 640, 640, 95, 639)


def test_gap_longer_than_reread_window_includes_published_qfq_anchor(tmp_path):
    dates = tuple(date(2026, 8, 25) + timedelta(days=i) for i in range(10))
    config = HistorySyncConfiguration(tmp_path, sessions=10, reread_sessions=2, minimum_free_bytes=0)
    first = run_history_sync(config, _router(Baseline(dates), Prices()), clock=lambda: NOW)
    assert first.state == "completed"
    shifted = tuple(day + timedelta(days=6) for day in dates)
    prices = Prices()
    result = run_history_sync(config, _router(Baseline(shifted), prices), clock=lambda: NOW)
    assert result.state == "completed"
    assert prices.calls == [shifted[-8:], shifted[-8:]]


def test_changed_price_contract_does_not_resume_legacy_short_batches():
    from trader.download.domain.history_control import HistorySyncCheckpoint
    from trader.download.infra.history_archive_sync import _matching_checkpoints

    previous = HistorySyncCheckpoint("sync-20260909-old", 1, "running", NOW, 1, 2, None)
    assert _matching_checkpoints((previous,), "sync-20260909-new") == ()
    assert _matching_checkpoints((previous,), previous.sync_identity) == (previous,)


def test_only_explicit_baseline_suspension_can_qualify_missing_tencent_prices():
    from trader.download.domain.history_price_qualification import qualify_history_pairs

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
    qfq = replace(raw, adjustment="qfq", preclose=None, pct_change=None, turnover=None)
    suspended = replace(evidence.batch.cells[0], qfq=qfq, status="supplier_marked_suspended")
    value = qualify_history_pairs(security.code, (day,), missing, (suspended,))
    assert value[0].status == "supplier_marked_suspended"
    assert qualify_history_pairs(security.code, (day,), missing)[0].status == "unknown_missing"
    with pytest.raises(RuntimeError, match="anchor_missing"):
        qualify_history_pairs(security.code, (day,), Prices().fetch_window(security, (day,)), (suspended,))


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


def test_history_st_reference_batches_report_distinct_counts(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)

    progress.publish(HistorySyncProgress("history_st_reference", "completed", 8, 5224))
    progress.publish(HistorySyncProgress("history_st_reference", "completed", 16, 5224))

    lines = capsys.readouterr().err.splitlines()
    assert lines == [
        "00:00:00 | 历史ST名单 | 完成 | 8/5224 (0.15%)",
        "00:00:00 | 历史ST名单 | 完成 | 16/5224 (0.31%)",
    ]


def test_gap_inventory_reports_missing_field_and_price_counts(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)
    progress.publish(
        HistorySyncProgress(
            "history_gap_inventory",
            "completed",
            2,
            2,
            gap_summary=HistoryGapSummary(1, 0),
        )
    )
    assert "统计历史缺口 | 完成 | 价格对待补 1 股日 | Tencent失败 0 股" in capsys.readouterr().err
