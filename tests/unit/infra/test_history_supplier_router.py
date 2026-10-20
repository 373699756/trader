from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.reference_fixtures import PreparedFixture
from tests.unit.entrypoints.test_history_sync_progress import _compact_log
from tests.unit.infra.research.test_history_sync import NOW, FakeSupplier, _download
from trader.download.domain.baostock_daily import (
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyFact,
    BaoStockSecurity,
)
from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import HistoryGapSummary, HistorySyncConfiguration, HistorySyncProgress
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryCell, PublishedHistoryWindow
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_industry_cache import HistoryIndustryQueryCache
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_supplier_router import FetchTencentHistoryWindow, HistorySupplierRouter
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot
from trader.training.infra.research.historical_industry_evidence import AuditPublishedHistoricalIndustryFacts


class Baseline(FakeSupplier):
    def load_industry_context(self, calendar, universe, *, industry_cache=None):
        return self.load_context(
            calendar.open_dates[-1], len(calendar.open_dates), universe=universe, industry_cache=industry_cache
        )

    def load_context(self, as_of, sessions, *, universe=None, industry_cache=None):
        self.industry_cache = industry_cache
        return super().load_context(as_of, sessions, universe=universe)

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


def test_history_context_uses_the_current_exchange_universe() -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = _current_universe("600001", "302132")
    context = _router(Baseline(dates), Prices(), universe).load_context(dates[-1], len(dates))

    assert context.universe == tuple(sorted(universe, key=lambda item: item.code))
    assert context.universe[0].board == "chinext"
    assert HISTORY_UNIVERSE_CONTRACT in context.source_versions.dependency_versions


def test_st_check_uses_current_check_day_instead_of_resumed_price_cutoff():
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    checked = date(2026, 10, 18)
    prices = Prices()
    prices.dates = dates
    context = HistorySupplierRouter(
        Baseline(dates),
        prices,
        _prepared(
            _current_universe("600001"), dates, NOW.replace(year=checked.year, month=checked.month, day=checked.day)
        ),
    ).load_context(dates[-1], len(dates))
    assert context.calendar.open_dates == dates
    assert context.st_evidence[0].checked_on == checked


@pytest.mark.parametrize("positions,expected", (((8, 9), 5), ((0, 9), 10), ((-1, 9), 2000)))
def test_sparse_gap_request_count_covers_date_span(positions, expected):
    calendar = tuple(date(2026, 10, 1) + timedelta(days=i) for i in range(10))
    requested = tuple(calendar[0] + timedelta(days=i) for i in positions)

    class RecordingPrices(Prices):
        def fetch_window(self, security, dates, **kwargs):
            assert kwargs == {"request_count": expected, "allow_interior_padding": True}
            return super().fetch_window(security, dates, **kwargs)

    assert FetchTencentHistoryWindow(RecordingPrices(), _current_universe("600001")[0], requested, calendar)


def test_prepared_calendar_is_shared_with_prices_and_industry_query() -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = _current_universe("600001")
    events = []

    class TencentCalendar(Prices):
        def load_history_context(self, as_of, sessions, selected, *, calendar):
            events.append("tencent_calendar")
            return super().load_history_context(as_of, sessions, selected, calendar=calendar)

    class IndustryOnly:
        def load_industry_context(self, calendar, selected, *, industry_cache=None):
            events.append("industry")
            assert calendar.open_dates == dates
            assert selected == universe
            return FakeSupplier(dates).load_context(dates[-1], len(dates), universe=selected)

    context = HistorySupplierRouter(IndustryOnly(), TencentCalendar(), _prepared(universe, dates)).load_context(
        dates[-1], len(dates)
    )
    assert events == ["tencent_calendar", "industry"]
    assert context.calendar.open_dates == dates
    assert any(name == "tencent_calendar" for name, _ in context.source_versions.dependency_versions)


def test_history_router_forwards_the_explicit_industry_query_cache(tmp_path) -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    baseline = Baseline(dates)
    universe = _current_universe("600001")
    cache = HistoryIndustryQueryCache(tmp_path / "industry_queries.sqlite3")
    router = HistorySupplierRouter(baseline, Prices(), _prepared(universe, dates), cache)
    router.load_context(dates[-1], len(dates))
    assert baseline.industry_cache is cache


def test_baostock_login_timeout_falls_back_to_tencent_price_context(tmp_path) -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = _current_universe("600001")

    class LoginTimeout(Baseline):
        def load_context(self, *_args, **_kwargs):
            raise RuntimeError("supplier_login_timeout")

    class TencentContext(Prices):
        def load_history_context(self, as_of, sessions, selected, *, calendar):
            context = FakeSupplier(dates).load_context(as_of, sessions, universe=selected)
            return replace(context, industry_intervals=())

    router = HistorySupplierRouter(LoginTimeout(dates), TencentContext(), _prepared(universe, dates))
    context = router.load_context(dates[-1], len(dates))

    assert context.calendar.open_dates == dates
    assert context.universe == universe
    assert context.industry_intervals == ()
    result = RunHistorySync(
        HistorySyncConfiguration(
            tmp_path,
            sessions=len(dates),
            reread_sessions=2,
            minimum_free_bytes=0,
            reference_root=(tmp_path).parent / "reference",
            training_root=(tmp_path).parent / "models",
        ),
        router,
        clock=lambda: _at(dates[-1]),
    )
    assert result.state == "completed"
    assert result.training_due is False and result.training_due_reason == "data_incomplete"


def test_non_login_context_failure_does_not_hide_reference_error() -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))

    class IndustryFailure(Baseline):
        def load_context(self, *_args, **_kwargs):
            raise RuntimeError("industry_query_failed")

    with pytest.raises(RuntimeError, match="industry_query_failed"):
        _router(IndustryFailure(dates), Prices()).load_context(dates[-1], len(dates))


@pytest.mark.parametrize("count", (5, 639, 640, 641, 2000))
def test_tencent_first_fetches_one_whole_window(count):
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(count))
    baseline, prices = Baseline(dates), Prices()
    router = _router(baseline, prices)
    context = router.load_context(dates[-1], count)
    security = context.universe[0]
    result = router.fetch_tencent_window(security, dates)
    assert tuple(cell.trade_date for cell in result.cells) == dates
    assert prices.calls == [dates]
    assert baseline.calls == []
    assert context.source_versions != baseline.load_context(dates[-1], count).source_versions


@pytest.mark.parametrize("incoming", ("missing", "qfq_basis", "raw"))
def test_existing_prices_win_over_recovery_and_new_qfq_values(tmp_path: Path, incoming):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(
        tmp_path,
        sessions=3,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    assert RunHistorySync(config, _router(Baseline(dates), Prices()), clock=lambda: _at(dates[-1])).state == "completed"
    before = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    rows = HistorySnapshotReader(tmp_path).read_day(dates[-1], before)
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline = Baseline(new_dates)
    result = RunHistorySync(
        config,
        _router(baseline, Prices(shift=1.0, missing=incoming == "missing", conflict=incoming == "raw")),
        clock=lambda: _at(new_dates[-1]),
    )
    state = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state()
    if incoming == "raw":
        assert result.state == "completed" and result.unresolved_price_cells == 2
        assert HistorySnapshotReader(tmp_path).read_day(dates[-1], before) == rows
        return
    assert result.state == "completed" and result.unresolved_price_cells == (2 if incoming == "missing" else 0)
    assert state.active_snapshot != before
    assert not any(code == "raw" for code, _dates in baseline.calls)
    assert rows[0].is_st is False
    assert rows[0].cell.qfq.close_price == 9.5
    current = HistorySnapshotReader(tmp_path)
    assert current.read_day(dates[-1], state.active_snapshot)[0].cell == rows[0].cell
    tail = current.read_day(new_dates[-1], state.active_snapshot)[0].cell
    if incoming == "missing":
        assert tail.status == "qfq_missing" and tail.qfq is None
    else:
        assert tail.qfq is not None and tail.qfq.close_price == 11.0


def test_bootstrap_wires_routing_to_real_snapshot_publication(tmp_path, monkeypatch):
    from trader.bootstrap import ExecuteHistoryDownload

    dates = (date(2026, 9, 8), date(2026, 9, 9))
    baseline = Baseline(dates)

    class Session(Baseline):
        def __enter__(self):
            return baseline

        def __exit__(self, *_args):
            pass

    supplier_settings = {}

    def build_supplier(configuration, **kwargs):
        supplier_settings["configuration"] = configuration
        supplier_settings.update(kwargs)
        return Session(dates)

    monkeypatch.setattr("trader.bootstrap.BaoStockHistorySupplier", build_supplier)
    monkeypatch.setattr("trader.bootstrap.TencentQfqSupplier", lambda _dependencies: Prices())
    monkeypatch.setattr("trader.bootstrap.HistoryStNameSource", lambda *_args, **_kwargs: StSource())
    monkeypatch.setattr(
        "trader.bootstrap.LoadCurrentAShareUniverse",
        lambda *_args: _current_universe("600001", "600002"),
    )
    result = ExecuteHistoryDownload(
        HistorySyncConfiguration(
            tmp_path,
            sessions=2,
            reread_sessions=2,
            minimum_free_bytes=0,
            reference_root=(tmp_path).parent / "reference",
            training_root=(tmp_path).parent / "models",
        ),
        reference=replace(_prepared(_current_universe("600001", "600002"), dates), industry_version="fixture"),
        clock=lambda: _at(dates[-1]),
    )
    assert result.state == "completed"
    assert baseline.calls == []
    assert supplier_settings["configuration"].supplier_timeout_seconds == 45.0
    assert supplier_settings["configuration"].supplier_retries == 2
    assert "price_policy" not in supplier_settings
    snapshot = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    assert snapshot is not None
    rows = HistorySnapshotReader(tmp_path).read_day(dates[-1], snapshot)
    assert rows[0].is_st is False
    assert rows[0].cell.qfq.close_price == 9.5


def test_valid_increment_requests_only_tail_and_publishes_new_day(tmp_path):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(
        tmp_path,
        sessions=3,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    assert RunHistorySync(config, _router(Baseline(dates), Prices()), clock=lambda: _at(dates[-1])).state == "completed"
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline, prices = Baseline(new_dates), Prices()
    result = RunHistorySync(config, _router(baseline, prices), clock=lambda: _at(new_dates[-1]))
    assert result.state == "completed"
    assert prices.calls == [new_dates, new_dates]
    assert baseline.calls == []
    snapshot = HistoryControlIndex(tmp_path / "catalog.sqlite3").load_state().active_snapshot
    assert snapshot.sequence == 2
    assert len(HistorySnapshotReader(tmp_path).read_day(new_dates[-1], snapshot)) == 2


def test_exchange_universe_shrink_publishes_only_current_codes(tmp_path) -> None:
    dates = tuple(date(2026, 8, 10) + timedelta(days=i) for i in range(61))
    config = HistorySyncConfiguration(
        tmp_path / "baostock",
        sessions=61,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path / "baostock").parent / "reference",
        training_root=(tmp_path / "baostock").parent / "models",
    )
    full = _current_universe("600001", "600002")
    assert (
        RunHistorySync(
            config, _router(Baseline(dates, industry="银行"), Prices(), full), clock=lambda: _at(dates[-1])
        ).state
        == "completed"
    )

    current = _current_universe("600001")
    result = RunHistorySync(
        config, _router(Baseline(dates, industry="银行"), Prices(), current), clock=lambda: _at(dates[-1])
    )

    assert result.state == "completed"
    archive = SQLitePublishedHistorySnapshot(config.archive_root)
    manifest = archive.manifest()
    assert manifest is not None
    assert manifest.universe_codes == ("600001",)
    assert tuple(window.code for window in archive.iter_windows(manifest, sessions=61)) == ("600001",)
    assert tuple(window.code for window in archive.read_windows(manifest, ("600001", "600002"), sessions=61)) == (
        "600001",
    )
    snapshot = HistoryControlIndex(config.archive_root / "catalog.sqlite3").load_state().active_snapshot
    assert snapshot is not None
    assert tuple(row.code for row in HistorySnapshotReader(config.archive_root).read_day(dates[-1], snapshot)) == (
        "600001",
    )
    training = SQLiteHistoryTrainingInputSnapshot.open(config.archive_root)
    assert training.snapshot.training_codes == ("600001",)
    training_rows = tuple(training.iter_training_rows(frozenset(dates)))
    assert len(training_rows) == len(dates)
    assert {row.code for row in training_rows} == {"600001"}
    industry_report = AuditPublishedHistoricalIndustryFacts(config.archive_root)
    assert industry_report.sources[0].sampled_codes == 1
    assert len(industry_report.merged_fact_hashes) == 1


def test_unmarked_supplier_universe_shrink_still_fails_closed(tmp_path) -> None:
    dates = tuple(date(2026, 10, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(
        tmp_path,
        sessions=3,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    assert RunHistorySync(config, Baseline(dates), clock=lambda: _at(dates[-1])).state == "completed"

    class Regressed(Baseline):
        def load_context(self, as_of, sessions, *, universe=None, industry_cache=None):
            context = super().load_context(as_of, sessions, universe=universe)
            return replace(context, universe=(context.universe[0],))

    result = RunHistorySync(config, Regressed(dates), clock=lambda: _at(dates[-1]))

    assert result.state == "failed"
    assert result.reason == "supplier_universe_regressed"


def test_new_stock_short_history_and_existing_stock_both_use_tencent(tmp_path):
    from trader.download.infra.history_download_index import HistoryDownloadIndex
    from trader.download.infra.history_sync_runner import _download_tasks

    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    baseline, prices = Baseline(dates), Prices()
    router = _router(baseline, prices)
    context = router.load_context(dates[-1], 2000)
    new_stock = replace(context.universe[1], listed_on=dates[-639])
    context = replace(context, universe=(context.universe[0], new_stock))
    control = HistoryControlIndex(tmp_path / "catalog.sqlite3")
    control.initialize()
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3")
    tasks = _download_tasks(context, control, None, index, 5)
    assert tasks[0].dates == dates
    assert tasks[1].dates == dates[-639:]
    assert router.fetch_tencent_window(context.universe[0], dates).cells
    assert router.fetch_tencent_window(new_stock, dates[-639:]).cells
    assert tuple(map(len, prices.calls)) == (2000, 639)


def test_gap_longer_than_reread_window_includes_published_qfq_anchor(tmp_path):
    dates = tuple(date(2026, 8, 25) + timedelta(days=i) for i in range(10))
    config = HistorySyncConfiguration(
        tmp_path,
        sessions=10,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path).parent / "reference",
        training_root=(tmp_path).parent / "models",
    )
    first = RunHistorySync(config, _router(Baseline(dates), Prices()), clock=lambda: _at(dates[-1]))
    assert first.state == "completed"
    shifted = tuple(day + timedelta(days=6) for day in dates)
    prices = Prices()
    result = RunHistorySync(config, _router(Baseline(shifted), prices), clock=lambda: _at(shifted[-1]))
    assert result.state == "completed"
    assert prices.calls == [shifted[-8:], shifted[-8:]]


def test_only_explicit_baseline_suspension_can_qualify_missing_tencent_prices():
    from trader.download.domain.history_price_qualification import QualifyHistoryPairs

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
    value = QualifyHistoryPairs(security.code, (day,), missing, (suspended,))
    assert value[0].status == "supplier_marked_suspended"
    existing = PublishedHistoryWindow(security.code, (ProjectHistoryCell(suspended),))
    assert QualifyHistoryPairs(security.code, (day,), existing) == (suspended,)
    assert QualifyHistoryPairs(security.code, (day,), missing)[0].status == "unknown_missing"
    with pytest.raises(RuntimeError, match="anchor_missing"):
        QualifyHistoryPairs(security.code, (day,), Prices().fetch_window(security, (day,)), (suspended,))


def test_partial_whole_window_is_returned_once_for_baostock_supplement():
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(2000))
    prices = Prices(missing=True)
    router = _router(Baseline(dates), prices)
    context = router.load_context(dates[-1], 2000)
    result = router.fetch_tencent_window(context.universe[0], dates)
    assert prices.calls == [dates]
    assert all(cell.qfq is None for cell in result.cells)


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
    assert "来源 Tencent | 请求 5 日" in _compact_log(capsys.readouterr().err)


def test_failed_tencent_download_reports_the_bounded_reason(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)

    progress.publish(
        HistorySyncProgress(
            "tencent_history",
            "failed",
            1,
            2,
            "600001",
            supplier_source="tencent",
            requested_sessions=5,
            failure_reason="qfq_daily_query_failed",
        )
    )

    line = _compact_log(capsys.readouterr().err).strip()
    assert "sync history 股票进度 | 1/2" in line
    assert "处理 Tencent下载 | 结果 失败" in line
    assert "原因 qfq_daily_query_failed" in line


def test_history_st_reference_batches_report_distinct_counts(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)

    progress.publish(HistorySyncProgress("history_st_reference", "completed", 8, 5224))
    progress.publish(HistorySyncProgress("history_st_reference", "completed", 16, 5224))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert lines == [
        "00:00:00 | 历史ST名单 | 检查中 | 8/5224 (0.15%) | 阶段耗时 00:00:00",
        "00:00:00 | 历史ST名单 | 检查中 | 16/5224 (0.31%) | 阶段耗时 00:00:00",
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
    assert "统计历史缺口 | 完成 | 价格对待补 1 股日 | 本轮Tencent失败 0 股" in _compact_log(capsys.readouterr().err)


def test_active_gap_scan_prints_the_month_before_the_query_starts(capsys):
    progress = StderrHistorySyncProgress(monotonic=lambda: 0)

    progress.publish(HistorySyncProgress("history_active_gaps", "started", 88, 100, "2025-11"))
    progress.publish(HistorySyncProgress("history_active_gaps", "completed", 89, 100, "2025-11"))

    assert _compact_log(capsys.readouterr().err).splitlines() == [
        "00:00:00 | 活动历史缺口 | 开始 | 88/100 (88.00%) | 月份 2025-11 | 本月耗时 00:00:00",
        "00:00:00 | 活动历史缺口 | 完成 | 89/100 (89.00%) | 月份 2025-11 | 本月耗时 00:00:00",
    ]
