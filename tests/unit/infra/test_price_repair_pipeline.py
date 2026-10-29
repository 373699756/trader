"""Second-source publication, restart and cross-basis regression boundaries."""

from dataclasses import replace
from datetime import date, datetime

from tests.component.test_qfq_windows import DAYS, SHANGHAI, Supplier, _updater, _window
from tests.helpers.history import RunHistoryFixture
from tests.unit.infra.research.test_history_sync import FakeSupplier, _download
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_sync import HistorySyncConfiguration
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.infra.sqlite_connection import SqliteSession


class RepairHistory(FakeSupplier):
    fail_repair = False

    def fetch_tencent_window(self, security, dates):
        self.events.append("tencent")
        cells = tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells)
        if security.code == "600001":
            cells = (replace(cells[0], status="qfq_missing", qfq=None), *cells[1:])
            raise HistorySupplierPendingError(PublishedHistoryWindow(security.code, cells), "tencent_qfq_invalid_price", dates[:1])
        return PublishedHistoryWindow(security.code, cells)

    def fetch_baostock_window(self, security, dates):
        self.events.append("baostock")
        if self.fail_repair:
            raise RuntimeError("supplier_query_failed")
        return PublishedHistoryWindow(security.code, tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, 2.0).batch.cells))


def _config(root, days):
    return HistorySyncConfiguration(root / "history", root / "reference", sessions=len(days), reread_sessions=1, minimum_free_bytes=0)


def _now(day):
    return datetime.combine(day, datetime.min.time(), tzinfo=SHANGHAI).replace(hour=20)


def test_history_waits_for_tencent_and_replaces_entire_basis(tmp_path):
    days = (date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 5))
    supplier = RepairHistory(days)
    config = _config(tmp_path, days)
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.events == ["tencent", "tencent", "baostock"]
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3").load_state().published_snapshot
    for day in days:
        rows = HistorySnapshotReader(config.history_root).read_day(day, snapshot)
        cell = next(row.cell for row in rows if row.code == "600001")
        assert cell.qfq.close_price == day.day + 2.0
    assert HistoryDownloadIndex(config.history_root / "catalog.sqlite3").gap_codes() == frozenset()
    assert HistoryPriceRepair(config.history_root).source("600001", snapshot.sequence) == "baostock"


def test_old_gap_is_not_retried_until_explicit_and_old_snapshot_is_immutable(tmp_path):
    days = (date(2026, 1, 2), date(2026, 1, 5))
    supplier = RepairHistory(days)
    supplier.fail_repair = True
    config = _config(tmp_path, days)
    first = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert first.unresolved_price_cells == 1
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3").load_state().published_snapshot
    previous = HistorySnapshotReader(config.history_root).read_day(days[0], snapshot)
    supplier.events.clear()
    RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert supplier.events == []
    supplier.fail_repair = False
    repaired = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert repaired.unresolved_price_cells == 0
    assert supplier.events == ["baostock"]
    assert HistorySnapshotReader(config.history_root).read_day(days[0], snapshot) == previous
    with SqliteSession(config.history_root / "catalog.sqlite3") as connection:
        assert [tuple(row) for row in connection.run("SELECT state,resolved_source FROM price_repair_attempt")] == [("resolved", "baostock")]


def test_history_replays_interrupted_whole_stock_without_another_request(tmp_path, monkeypatch):
    from trader.download.infra.history_prices import HistoryPricePartition

    days = (date(2025, 12, 31), date(2026, 1, 2))
    config = _config(tmp_path, days)
    supplier = RepairHistory(days)
    original = HistoryPricePartition.replace_unpublished_code

    def fail_second_year(self, revisions, sequence):
        if revisions[0].trade_date.year == 2026:
            raise RuntimeError("fixture_write_interrupted")
        return original(self, revisions, sequence)

    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", fail_second_year)
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1])).state == "failed"
    assert HistoryControlIndex(config.history_root / "catalog.sqlite3").load_state().published_snapshot is None
    supplier.events.clear()
    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", original)
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.events == []


def test_market_second_phase_and_explicit_only_retry(tmp_path):
    events = []

    class Tencent(Supplier):
        def fetch_window(self, security, dates):
            events.append("tencent")
            window = _window(security.code, dates)
            raise HistorySupplierPendingError(replace(window, cells=(replace(window.cells[0], status="qfq_missing", qfq=None), *window.cells[1:])), "tencent_qfq_invalid_price", dates[:1])

    class Bao:
        fail = True

        def fetch_baostock_window(self, security, dates):
            events.append("baostock")
            if self.fail:
                raise RuntimeError("supplier_query_failed")
            window = _window(security.code, dates)
            return replace(window, cells=tuple(replace(cell, qfq=replace(cell.qfq, open_price=20.0, high_price=20.2, low_price=19.8, close_price=20.0)) for cell in window.cells))

    bao = Bao()
    updater = replace(_updater(tmp_path, Tencent()), repair_supplier=bao)
    first = updater.execute(_now(DAYS[-1]))
    assert first.pending_codes == 1 and events == ["tencent", "baostock"]
    events.clear()
    updater.execute(_now(DAYS[-1]))
    assert events == ["tencent"]
    events.clear()
    bao.fail = False
    fixed = replace(updater, repair_only=True).execute(_now(DAYS[-1]))
    assert fixed.pending_codes == 0 and events == ["baostock"]
    assert updater.v2.price_issues() == ()
    assert all(cell.qfq.close_price == 20 for cell in updater.v2.read_code("600001").cells)
    assert len(updater.v3.read_code("600001").cells) == 61
    source = updater.v2.source_identity("600001")
    assert source.startswith("baostock:") and updater.v2.completed(DAYS[-1], "600001", source)
    events.clear()
    assert updater.execute(_now(DAYS[-1])).skipped_codes == 1
    assert events == []


def test_new_history_day_uses_tencent_tail_then_same_source_bao_tail(tmp_path):
    days = (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    supplier = RepairHistory(days[:2])
    config = _config(tmp_path, days[:2])
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(days[1])).unresolved_price_cells == 0
    supplier.dates = days[1:]
    supplier.events.clear()
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.events[-1] == "baostock"
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3").load_state().published_snapshot
    for day in days[1:]:
        rows = HistorySnapshotReader(config.history_root).read_day(day, snapshot)
        assert next(row.cell for row in rows if row.code == "600001").qfq.close_price == day.day + 2


def test_market_conflicting_raw_preserves_previous_pair_and_completion_is_not_claimed(tmp_path):
    from trader.download.domain.price_repair import PriceIssue

    updater = _updater(tmp_path, Supplier())
    assert updater.execute(_now(DAYS[-1])).completed_codes == 1
    previous = updater.v2.read_code("600001")
    updater.v2.record_price_issue(PriceIssue("600001", DAYS[-1:], "fixture_raw_conflict"))

    class Bao:
        def fetch_baostock_window(self, security, dates):
            return _window(security.code, dates, price=12.0)

    result = replace(updater, repair_supplier=Bao(), repair_only=True).execute(_now(DAYS[-1]))
    assert result.pending_codes == 1
    assert updater.v2.read_code("600001") == previous
    assert not updater.v2.completed(DAYS[-1], "600001", updater.v2.source_identity("600001"))


def test_explicit_history_repair_does_not_publish_unfinished_initial_download(tmp_path):
    from trader.download.infra.history_download_index import HistoryDownloadBatch, HistoryDownloadTask

    days = (date(2026, 1, 2), date(2026, 1, 5))
    config = _config(tmp_path, days)
    supplier = RepairHistory(days)
    RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    state = HistoryControlIndex(config.history_root / "catalog.sqlite3").load_published_state()
    HistoryDownloadIndex(config.history_root / "catalog.sqlite3").begin(HistoryDownloadBatch(state.snapshot.sequence + 1, state.source.content_hash, state.calendar.content_hash, state.universe.content_hash, days[-1]), (HistoryDownloadTask("600001", days),))
    supplier.events.clear()
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert result.state == "blocked" and result.reason == "history_download_incomplete"
    assert supplier.events == [] and result.snapshot_hash == state.snapshot.content_hash
