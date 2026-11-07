from __future__ import annotations

from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace
import json

from datetime import date, timedelta

from pathlib import Path

import pytest

from tests.helpers.download.history_fixtures import RunHistoryFixture


from tests.helpers.download.history.history_sync_progress import _compact_log

from tests.helpers.download.history.infra.history_sync import NOW


from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT


from trader.download.domain.history_sync import HistoryGapSummary, HistorySyncProgress

from tests.helpers.download.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration

from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryCell, PublishedHistoryWindow

from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress

from trader.download.infra.history_control_index import HistoryControlIndex

from trader.download.infra.download_settings import LoadQfqWindowConfiguration

from trader.download.infra.industry_query_index import IndustrySourceDocument, SQLiteIndustryQueryIndex

from trader.download.infra.history_snapshot_reader import HistorySnapshotReader

from trader.download.infra.history_supplier_router import FetchTencentHistoryWindow, HistorySupplierRouter

from trader.download.infra.published_history_snapshot import SQLitePublishedHistorySnapshot

from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

from trader.training.infra.research.historical_industry_evidence import AuditPublishedHistoricalIndustryFacts

from tests.helpers.download.history.history_supplier_router import (
    Baseline,
    Prices,
    StSource,
    _at,
    _current_universe,
    _prepared,
    _router,
)


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


@pytest.mark.parametrize("positions,expected", (((8, 9), 7), ((0, 9), 12), ((-1, 9), 2000)))
def test_sparse_gap_request_count_covers_date_span(positions, expected):
    calendar = tuple(date(2026, 10, 1) + timedelta(days=i) for i in range(10))
    requested = tuple(calendar[0] + timedelta(days=i) for i in positions)

    class RecordingPrices(Prices):
        def fetch_window(self, security, dates, **kwargs):
            assert kwargs == {"request_count": expected, "allow_interior_padding": True}
            return super().fetch_window(security, dates, **kwargs)

    assert FetchTencentHistoryWindow(RecordingPrices(), _current_universe("600001")[0], requested, calendar)


def test_prepared_calendar_and_universe_are_taken_only_from_tencent_context() -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = _current_universe("600001")
    events = []

    class TencentCalendar(Prices):
        def load_history_context(self, as_of, sessions, selected, *, calendar):
            events.append("tencent_calendar")
            return super().load_history_context(as_of, sessions, selected, calendar=calendar)

    class ForbiddenBaseline:
        def load_context(self, *_args, **_kwargs):
            raise AssertionError("BaoStock must not prepare history references")

        def load_industry_context(self, *_args, **_kwargs):
            raise AssertionError("BaoStock industry requests have been removed")

    context = HistorySupplierRouter(ForbiddenBaseline(), TencentCalendar(), _prepared(universe, dates)).load_context(
        dates[-1], len(dates)
    )
    assert events == ["tencent_calendar"]
    assert context.calendar.open_dates == dates
    assert context.universe == universe
    assert context.industry_intervals == ()
    assert context.industry_readiness is not None
    assert context.industry_readiness.requested_codes == 1
    assert not context.industry_readiness.ready
    assert any(name == "tencent_calendar" for name, _ in context.source_versions.dependency_versions)


def test_history_router_inspects_the_explicit_industry_query_index_without_promoting_change_date(tmp_path) -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    baseline = Baseline(dates)
    universe = _current_universe("600001")
    cache = SQLiteIndustryQueryIndex(tmp_path / "industry.sqlite3", "cninfo", sqlite_policy=SqlitePolicyFixture())
    payload = json.dumps({
        "resultcode": 200, "count": 1, "records": [{
            "SECCODE": "600001", "SECNAME": "fixture", "ORGNAME": "fixture organization",
            "VARYDATE": dates[0].isoformat(), "F001V": "008001",
            "F002V": "中国上市公司协会上市公司行业分类标准", "F003V": "J661",
            "F004V": "金融业", "F005V": None, "F006V": "货币金融服务",
            "F007V": "货币银行服务", "F008C": "1",
        }],
    }, ensure_ascii=False).encode()
    document = IndustrySourceDocument("cninfo:p_stock2110:600001", dates[0], dates[-1], _at(dates[-1]), payload)
    cache.save_source_documents((document,))
    router = HistorySupplierRouter(baseline, Prices(), _prepared(universe, dates), cache)
    context = router.load_context(dates[-1], len(dates))
    assert context.industry_readiness is not None
    assert context.industry_readiness.candidate_codes == 1
    assert context.industry_readiness.point_in_time_codes == 0
    assert not context.industry_readiness.ready
    assert context.industry_intervals == ()
    assert cache.read_source_document(document.request_key, _at(dates[-1])) == document
    assert baseline.calls == []


def test_price_publication_does_not_call_baostock_context_when_industry_is_not_ready(tmp_path) -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    universe = _current_universe("600001")

    class ForbiddenBaseline(Baseline):
        def load_context(self, *_args, **_kwargs):
            raise AssertionError("BaoStock context must not be queried")

    router = HistorySupplierRouter(ForbiddenBaseline(dates), Prices(), _prepared(universe, dates))
    context = router.load_context(dates[-1], len(dates))

    assert context.calendar.open_dates == dates
    assert context.universe == universe
    assert context.industry_intervals == ()
    assert context.industry_readiness is not None
    assert context.industry_readiness.point_in_time_codes == 0
    result = RunHistoryFixture(
        HistorySyncConfiguration(
            tmp_path,
            sessions=len(dates),
            reread_sessions=2,
            minimum_free_bytes=0,
            reference_root=(tmp_path).parent / "reference",
        ),
        router,
        clock=lambda: _at(dates[-1]),
    )
    assert result.state == "completed"
    assert result.published and not result.needs_retry
    assert result.industry_readiness is not None
    assert not result.industry_readiness.ready


def test_invalid_industry_query_index_preserves_original_and_reports_unready_prices(tmp_path) -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))

    path = tmp_path / "industry.sqlite3"
    path.write_bytes(b"invalid sqlite evidence")
    cache = SQLiteIndustryQueryIndex(path, "cninfo", sqlite_policy=SqlitePolicyFixture())
    class ForbiddenBaseline(Baseline):
        def load_context(self, *_args, **_kwargs):
            raise AssertionError("Industry failure must not query BaoStock")

    baseline = ForbiddenBaseline(dates)
    router = HistorySupplierRouter(baseline, Prices(), _prepared(_current_universe("600001"), dates), cache)
    context = router.load_context(dates[-1], len(dates))
    assert context.industry_readiness is not None
    assert context.industry_readiness.failure_category == "RuntimeError"
    assert context.industry_readiness.point_in_time_codes == 0
    assert not context.industry_readiness.ready
    assert context.industry_intervals == ()
    assert baseline.calls == []
    assert path.read_bytes() == b"invalid sqlite evidence"


def test_cancelled_history_industry_inspection_is_not_downgraded() -> None:
    dates = (date(2026, 10, 8), date(2026, 10, 9))
    baseline = Baseline(dates)
    router = HistorySupplierRouter(
        baseline, Prices(), _prepared(_current_universe("600001"), dates), cancel_requested=lambda: True
    )
    with pytest.raises(RuntimeError, match="supplier_cancelled"):
        router.load_context(dates[-1], len(dates))
    assert baseline.calls == []


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
    assert context.source_versions.sdk_version.endswith("+capco-industry")


@pytest.mark.parametrize("incoming", ("missing", "raw"))
def test_existing_prices_survive_missing_or_conflicting_raw_input(tmp_path: Path, incoming):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(tmp_path).parent / "reference"
    )
    assert (
        RunHistoryFixture(config, _router(Baseline(dates), Prices()), clock=lambda: _at(dates[-1])).state == "completed"
    )
    before = HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    rows = HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).read_day(dates[-1], before)
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline = Baseline(new_dates)
    result = RunHistoryFixture(
        config,
        _router(baseline, Prices(shift=1.0, missing=incoming == "missing", conflict=incoming == "raw")),
        clock=lambda: _at(new_dates[-1]),
    )
    state = HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state()
    if incoming == "raw":
        assert result.state == "completed" and result.unresolved_price_cells == 2
        assert HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).read_day(dates[-1], before) == rows
        return
    assert result.state == "completed" and result.unresolved_price_cells == (2 if incoming == "missing" else 0)
    assert state.published_snapshot != before
    assert not any(code == "raw" for code, _dates in baseline.calls)
    assert rows[0].is_st is False
    assert rows[0].cell.qfq.close_price == 9.5
    current = HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture())
    assert current.read_day(dates[-1], state.published_snapshot)[0].cell == rows[0].cell
    tail = current.read_day(new_dates[-1], state.published_snapshot)[0].cell
    assert tail.status == "qfq_missing" and tail.qfq is None


def test_bootstrap_wires_routing_to_real_snapshot_publication(tmp_path, monkeypatch):
    from trader.bootstrap import ExecuteHistoryDownload
    from trader.download.infra.history_industry_preparation import PrepareHistoricalIndustries
    from trader.download.infra.reference_index import SQLiteReferenceIndex

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
    monkeypatch.setattr("trader.bootstrap.TencentQfqSupplier", lambda _dependencies, _options: Prices())
    monkeypatch.setattr("trader.bootstrap.HistoryStNameSource", lambda *_args, **_kwargs: StSource())

    class HistoricalIndustries:
        def __init__(self, *_args, **_kwargs):
            pass

        def prepare(self, universe, calendar, observed_at, *, progress=None):
            return PrepareHistoricalIndustries(
                None, universe, calendar, observed_at, cancel_requested=lambda: False, progress=progress,
            )

    monkeypatch.setattr("trader.bootstrap.OfficialIndustryHistory", HistoricalIndustries)
    monkeypatch.setattr(
        "trader.bootstrap.LoadCurrentAShareUniverse",
        lambda *_args: _current_universe("600001", "600002"),
    )
    reference = replace(_prepared(_current_universe("600001", "600002"), dates), industry_version="fixture")
    SQLiteReferenceIndex(tmp_path.parent / "reference" / "company_st.sqlite3", sqlite_policy=SqlitePolicyFixture()).publish(reference, reference.st_evidence)
    result = ExecuteHistoryDownload(
        HistorySyncConfiguration(
            tmp_path,
            sessions=2,
            reread_sessions=2,
            minimum_free_bytes=0,
            reference_root=(tmp_path).parent / "reference",
        ),
        qfq_configuration=LoadQfqWindowConfiguration(Path(__file__).resolve().parents[4]),
        shutdown_timeout_seconds=2.0,
        reference=reference,
        clock=lambda: _at(dates[-1]), sqlite_policy=SqlitePolicyFixture(),
    )
    assert result.state == "completed"
    assert result.industry_readiness is not None
    assert not result.industry_readiness.ready
    assert baseline.calls == []
    assert supplier_settings["configuration"].supplier_timeout_seconds == 45.0
    assert supplier_settings["configuration"].supplier_retries == 2
    assert supplier_settings["price_policy"].retries == 2
    assert supplier_settings["vendor_lock"] is not None
    snapshot = HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    assert snapshot is not None
    rows = HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).read_day(dates[-1], snapshot)
    assert rows[0].is_st is False
    assert rows[0].cell.qfq.close_price == 9.5


def test_valid_increment_requests_only_tail_and_publishes_new_day(tmp_path):
    dates = tuple(date(2026, 9, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(tmp_path).parent / "reference"
    )
    assert (
        RunHistoryFixture(config, _router(Baseline(dates), Prices()), clock=lambda: _at(dates[-1])).state == "completed"
    )
    new_dates = tuple(day + timedelta(days=1) for day in dates)
    baseline, prices = Baseline(new_dates), Prices()
    result = RunHistoryFixture(config, _router(baseline, prices), clock=lambda: _at(new_dates[-1]))
    assert result.state == "completed"
    assert prices.calls == [new_dates, new_dates]
    assert baseline.calls == []
    snapshot = HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    assert snapshot.sequence == 2
    assert len(HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).read_day(new_dates[-1], snapshot)) == 2


def test_exchange_universe_shrink_publishes_only_current_codes(tmp_path) -> None:
    dates = tuple(date(2026, 8, 10) + timedelta(days=i) for i in range(61))
    config = HistorySyncConfiguration(
        tmp_path / "baostock",
        sessions=61,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(tmp_path / "baostock").parent / "reference",
    )
    full = _current_universe("600001", "600002")
    assert (
        RunHistoryFixture(
            config, _router(Baseline(dates, industry="银行"), Prices(), full), clock=lambda: _at(dates[-1])
        ).state
        == "completed"
    )

    current = _current_universe("600001")
    result = RunHistoryFixture(
        config, _router(Baseline(dates, industry="银行"), Prices(), current), clock=lambda: _at(dates[-1])
    )

    assert result.state == "completed"
    history = SQLitePublishedHistorySnapshot(config.history_root, sqlite_policy=SqlitePolicyFixture())
    manifest = history.manifest()
    assert manifest is not None
    assert manifest.universe_codes == ("600001",)
    assert tuple(window.code for window in history.iter_windows(manifest, sessions=61)) == ("600001",)
    assert tuple(window.code for window in history.read_windows(manifest, ("600001", "600002"), sessions=61)) == (
        "600001",
    )
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    assert snapshot is not None
    assert tuple(row.code for row in HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(dates[-1], snapshot)) == (
        "600001",
    )
    training = SQLiteHistoryTrainingInputSnapshot.open(config.history_root, sqlite_policy=SqlitePolicyFixture())
    assert training.snapshot.training_codes == ("600001",)
    training_rows = tuple(training.iter_training_rows(frozenset(dates)))
    assert len(training_rows) == len(dates)
    assert {row.code for row in training_rows} == {"600001"}
    industry_report = AuditPublishedHistoricalIndustryFacts(config.history_root, sqlite_policy=SqlitePolicyFixture())
    assert not training.has_industry_window(frozenset(dates), 61)
    assert training.industry_sample_upper_bounds(frozenset(dates), 61, frozenset(dates)) == ()
    assert industry_report.sources[0].sampled_codes == 1
    assert industry_report.sources[0].covered_trading_dates == 0
    assert industry_report.sources[0].missing_dates == len(dates)
    assert industry_report.sources[0].fact_hashes == ()
    assert industry_report.merged_fact_hashes == ()


def test_unmarked_supplier_universe_shrink_still_fails_closed(tmp_path) -> None:
    dates = tuple(date(2026, 10, 7) + timedelta(days=i) for i in range(3))
    config = HistorySyncConfiguration(
        tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(tmp_path).parent / "reference"
    )
    assert RunHistoryFixture(config, Baseline(dates), clock=lambda: _at(dates[-1])).state == "completed"

    class Regressed(Baseline):
        def load_context(self, as_of, sessions, *, universe=None):
            context = super().load_context(as_of, sessions, universe=universe)
            return replace(context, universe=(context.universe[0],))

    result = RunHistoryFixture(config, Regressed(dates), clock=lambda: _at(dates[-1]))

    assert result.state == "failed"
    assert result.reason == "supplier_universe_regressed"


def test_new_stock_short_history_and_existing_stock_both_use_tencent(tmp_path):
    from trader.download.infra.history_download_index import HistoryDownloadIndex
    from trader.download.domain.history_download import PlanHistoryDownloadTasks

    dates = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))
    baseline, prices = Baseline(dates), Prices()
    router = _router(baseline, prices)
    context = router.load_context(dates[-1], 2000)
    new_stock = replace(context.universe[1], listed_on=dates[-639])
    context = replace(context, universe=(context.universe[0], new_stock))
    control = HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    control.initialize()
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    tasks = PlanHistoryDownloadTasks(context, None, frozenset(), index.gaps_by_code(context.calendar.open_dates), 5)
    assert tasks[0].dates == dates
    assert tasks[1].dates == dates[-639:]
    assert router.fetch_tencent_window(context.universe[0], dates).cells
    assert router.fetch_tencent_window(new_stock, dates[-639:]).cells
    assert tuple(map(len, prices.calls)) == (2000, 639)


def test_gap_longer_than_reread_window_includes_published_qfq_anchor(tmp_path):
    dates = tuple(date(2026, 8, 25) + timedelta(days=i) for i in range(10))
    config = HistorySyncConfiguration(
        tmp_path, sessions=10, reread_sessions=2, minimum_free_bytes=0, reference_root=(tmp_path).parent / "reference"
    )
    first = RunHistoryFixture(config, _router(Baseline(dates), Prices()), clock=lambda: _at(dates[-1]))
    assert first.state == "completed"
    shifted = tuple(day + timedelta(days=6) for day in dates)
    prices = Prices()
    result = RunHistoryFixture(config, _router(Baseline(shifted), prices), clock=lambda: _at(shifted[-1]))
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
    assert "history·Tencent下载 00:00:00 | 失败 | 已确认 1/2" in line
    assert "处理 Tencent下载 | 结果 失败" in line
    assert "原因 qfq_daily_query_failed" in line


def test_history_st_reference_batches_report_distinct_counts(capsys):
    clock = [0.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])

    progress.publish(HistorySyncProgress("history_st_reference", "completed", 8, 5224))
    clock[0] = 1.0
    progress.publish(HistorySyncProgress("history_st_reference", "completed", 16, 5224))

    lines = _compact_log(capsys.readouterr().err).splitlines()
    assert lines == [
        "00:00:00 | history·历史ST名单 00:00:00 | 处理中 | 8/5224 (0.15%)",
        "00:00:01 | history·历史ST名单 00:00:01 | 处理中 | 16/5224 (0.31%)",
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
    assert "统计历史缺口 00:00:00 | 完成 | 价格对待补 1 股日 | 本轮Tencent失败 0 股" in _compact_log(capsys.readouterr().err)


def test_current_gap_scan_prints_the_month_before_the_query_starts(capsys):
    clock = [0.0]
    progress = StderrHistorySyncProgress(monotonic=lambda: clock[0])

    progress.publish(HistorySyncProgress("history_published_gaps", "started", 88, 100, "2025-11"))
    clock[0] = 1.0
    progress.publish(HistorySyncProgress("history_published_gaps", "completed", 89, 100, "2025-11"))

    assert _compact_log(capsys.readouterr().err).splitlines() == [
        "00:00:00 | history·活动历史缺口 00:00:00 | 开始 | 88/100 (88.00%) | 月份 2025-11",
        "00:00:01 | history·活动历史缺口 00:00:01 | 处理中 | 89/100 (89.00%) | 月份 2025-11",
    ]
