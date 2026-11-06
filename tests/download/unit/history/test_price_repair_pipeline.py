"""Second-source publication, restart and cross-basis regression boundaries."""

from tests.helpers.shared.configuration import SqlitePolicyFixture

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from tests.helpers.download.history.qfq_windows import DAYS, SHANGHAI, Supplier, _updater, _window
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.download.history.infra.history_sync import FakeSupplier, _download
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.history_price_qualification import HISTORY_UNIVERSE_CONTRACT
from tests.helpers.download.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration
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
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    for day in days:
        rows = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(day, snapshot)
        cell = next(row.cell for row in rows if row.code == "600001")
        assert cell.qfq.close_price == day.day + 2.0
    assert HistoryDownloadIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).gap_codes() == frozenset()
    assert HistoryPriceRepair(config.history_root, sqlite_policy=SqlitePolicyFixture()).source("600001", snapshot.sequence) == "baostock"


def test_explicit_repair_reuses_unchanged_month_proofs(tmp_path):
    from tests.helpers.download.history.infra.history_sync import ProgressRecorder

    days = (date(2026, 1, 5), date(2026, 2, 5), date(2026, 3, 5))

    class SameBasis(RepairHistory):
        def fetch_baostock_window(self, security, dates):
            if self.fail_repair:
                raise RuntimeError("supplier_query_failed")
            return PublishedHistoryWindow(security.code, tuple(
                ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells
            ))

    supplier = SameBasis(days)
    supplier.fail_repair = True
    config = _config(tmp_path, days)
    RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    control = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    previous = control.load_state().published_snapshot
    supplier.fail_repair = False
    progress = ProgressRecorder()
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True, progress=progress)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    current = control.load_state().published_snapshot
    assert current.partitions[1:] == previous.partitions[1:]
    sealed = [item for item in progress.values if item.stage == "sealing_partitions" and item.state == "completed"]
    assert [(item.current_item, item.total_units) for item in sealed] == [("prices_2026.sqlite3", 1)]


def test_old_gap_is_not_retried_until_explicit_and_old_snapshot_is_immutable(tmp_path):
    days = (date(2026, 1, 2), date(2026, 1, 5))
    supplier = RepairHistory(days)
    supplier.fail_repair = True
    config = _config(tmp_path, days)
    first = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert first.unresolved_price_cells == 1
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    previous = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(days[0], snapshot)
    supplier.events.clear()
    RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert supplier.events == []
    supplier.fail_repair = False
    repaired = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert repaired.unresolved_price_cells == 0
    assert supplier.events == ["baostock"]
    assert HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(days[0], snapshot) == previous
    with SqliteSession(config.history_root / "catalog.sqlite3") as connection:
        assert [tuple(row) for row in connection.run("SELECT state,resolved_source FROM price_repair_attempt")] == [("resolved", "baostock")]


def test_rolling_calendar_ignores_old_gap_and_fill_makes_no_supplier_requests(tmp_path):
    days = (date(2026, 1, 2), date(2026, 1, 5), date(2026, 1, 6))
    supplier = RepairHistory(days[:2])
    supplier.fail_repair = True
    config = _config(tmp_path, days[:2])
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(days[1])).unresolved_price_cells == 1
    path = config.history_root / "catalog.sqlite3"
    control = HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture())
    previous = control.load_state().published_snapshot
    reader = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture())
    old_cells = reader.read_day(days[0], previous)

    current_supplier = FakeSupplier(days)
    current = RunHistoryFixture(config, current_supplier, clock=lambda: _now(days[-1]))
    assert current.state == "completed" and current.unresolved_price_cells == 0
    assert current.reason is None and current.price_gap_summary.cells == 0
    current_supplier.calls.clear()
    current_supplier.events.clear()
    for repair_only in (False, True):
        repeated = RunHistoryFixture(config, current_supplier, clock=lambda: _now(days[-1]), repair_only=repair_only)
        assert repeated.state == "already_current" and repeated.unresolved_price_cells == 0
        assert repeated.reason is None and repeated.price_gap_summary.cells == 0
    assert current_supplier.events == [] and current_supplier.calls == []
    assert HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture()).gaps_by_code() == {"600001": days[:1]}
    assert reader.read_day(days[0], previous) == old_cells
    with SqliteSession(path) as connection:
        assert [row[0] for row in connection.run("SELECT state FROM price_repair_attempt")] == ["failed"]


@pytest.mark.parametrize("gap_index", (0, 9))
def test_baostock_repair_uses_tail_only_when_it_contains_every_gap(tmp_path, gap_index):
    days = tuple(date(2026, 1, 5) + timedelta(days=offset) for offset in range(12))
    gap_date = days[gap_index]
    requested = []

    class SameBasis(RepairHistory):
        def fetch_tencent_window(self, security, dates):
            cells = tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells)
            if security.code == "600001":
                cells = tuple(replace(cell, status="qfq_missing", qfq=None) if cell.trade_date == gap_date else cell for cell in cells)
                raise HistorySupplierPendingError(PublishedHistoryWindow(security.code, cells), "tencent_qfq_invalid_price", (gap_date,))
            return PublishedHistoryWindow(security.code, cells)

        def fetch_baostock_window(self, security, dates):
            if self.fail_repair:
                raise RuntimeError("supplier_query_failed")
            requested.append(dates)
            return PublishedHistoryWindow(security.code, tuple(
                ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells
            ))

    config = _config(tmp_path, days)
    supplier = SameBasis(days)
    supplier.fail_repair = True
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1])).unresolved_price_cells == 1
    path = config.history_root / "catalog.sqlite3"
    control = HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture())
    previous = control.load_state().published_snapshot
    reader = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture())
    old_cells = reader.read_day(gap_date, previous)
    # Reproduce a retained gap on an existing same-basis BaoStock window.
    with SqliteSession(path) as connection:
        connection.run("INSERT INTO price_source VALUES(?,?,?)", ("600001", previous.sequence, "baostock"))
    control.publish_file()
    supplier.fail_repair = False

    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    # A changed existing partial cell fails the exact-overlap check and refetches.
    assert requested == ([days] if gap_index == 0 else [days[-5:], days])
    assert reader.read_day(gap_date, previous) == old_cells
    current = control.load_state().published_snapshot
    assert next(row.cell.status for row in reader.read_day(gap_date, current) if row.code == "600001") == "complete"


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
    assert HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot is None
    supplier.events.clear()
    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", original)
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.events == []


def test_excluded_replacement_is_retained_without_decode_and_never_rebased_after_publication(tmp_path, monkeypatch):
    from trader.download.infra import history_price_repair
    from trader.download.infra.history_prices import HistoryPricePartition

    class Selected(RepairHistory):
        eligible_codes = ("600001", "600002")

        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            return replace(
                context,
                universe=tuple(item for item in context.universe if item.code in self.eligible_codes),
                st_evidence=tuple(
                    replace(item, status="clear" if item.code in self.eligible_codes else "unknown")
                    for item in context.st_evidence
                ),
                source_versions=replace(context.source_versions, dependency_versions=(HISTORY_UNIVERSE_CONTRACT,)),
            )

    days = (date(2025, 12, 31), date(2026, 1, 2))
    config = _config(tmp_path, days)
    supplier = Selected(days)
    original = HistoryPricePartition.replace_unpublished_code

    def interrupted(partition, revisions, sequence):
        if revisions[0].trade_date.year == 2026:
            raise RuntimeError("fixture_write_interrupted")
        return original(partition, revisions, sequence)

    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", interrupted)
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1])).state == "failed"
    path = config.history_root / "catalog.sqlite3"
    with SqliteSession(path) as connection:
        retained = tuple(tuple(row) for row in connection.run("SELECT * FROM price_replacement ORDER BY code,trade_date"))
    assert len(retained) == 2
    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", original)
    supplier.eligible_codes = ("600002",)
    supplier.events.clear()
    decode = history_price_repair.DecodePublishedHistoryCellPayload

    def selected_decode(code, *args):
        assert code in supplier.eligible_codes
        return decode(code, *args)

    monkeypatch.setattr(history_price_repair, "DecodePublishedHistoryCellPayload", selected_decode)
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.events == []
    with SqliteSession(path) as connection:
        assert tuple(tuple(row) for row in connection.run("SELECT * FROM price_replacement ORDER BY code,trade_date")) == retained
    snapshot = HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    reader = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture())
    assert {row.code for row in reader.read_day(days[-1], snapshot)} == {"600002"}

    supplier.eligible_codes = ("600001", "600002")
    rejected = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert rejected.state == "failed" and rejected.reason == "history_replacement_sequence_conflict"
    assert supplier.events == []
    assert HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot == snapshot
    with SqliteSession(path) as connection:
        assert tuple(tuple(row) for row in connection.run("SELECT * FROM price_replacement ORDER BY code,trade_date")) == retained


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
    assert supplier.events.count("tencent") == 2  # One request per stock, with no second pass.
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    for day in days[1:]:
        rows = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(day, snapshot)
        assert next(row.cell for row in rows if row.code == "600001").qfq.close_price == day.day + 2


@pytest.mark.parametrize(
    ("initial_source", "basis_changed", "incomplete"),
    (("tencent", True, False), ("baostock", False, False), ("baostock", True, False), ("baostock", True, True)),
)
def test_one_day_gap_replaces_all_2000_days_on_basis_change(tmp_path, initial_source, basis_changed, incomplete):
    days = tuple(
        day for day in (date(2018, 1, 1) + timedelta(days=offset) for offset in range(3000)) if day.weekday() < 5
    )[:2001]

    class DailySupplier(FakeSupplier):
        seed = True

        def load_context(self, as_of, sessions, **kwargs):
            context = super().load_context(as_of, sessions, **kwargs)
            return replace(context, universe=context.universe[:1], st_evidence=context.st_evidence[:1])

        def fetch_tencent_window(self, security, dates):
            self.calls.append(("tencent", dates))
            cells = tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells)
            if not self.seed or initial_source == "baostock":
                cells = (*cells[:-1], replace(cells[-1], status="qfq_missing", qfq=None))
                raise HistorySupplierPendingError(PublishedHistoryWindow(security.code, cells))
            return PublishedHistoryWindow(security.code, cells)

        def fetch_baostock_window(self, security, dates):
            self.calls.append(("baostock", dates))
            shift = 4.0 if not self.seed and basis_changed else 2.0
            cells = tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, shift).batch.cells)
            if not self.seed and incomplete and len(dates) == 2000:
                cells = (replace(cells[0], status="qfq_missing", qfq=None), *cells[1:])
            return PublishedHistoryWindow(security.code, cells)

    config = replace(_config(tmp_path, days[:2000]), reread_sessions=5)
    supplier = DailySupplier(days[:2000])
    first = RunHistoryFixture(config, supplier, clock=lambda: _now(days[1999]))
    assert first.state == "completed" and first.unresolved_price_cells == 0
    control = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    previous_snapshot = control.load_state().published_snapshot
    reader = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture())
    previous = reader.read_published_code_window("600001", days[:2000], previous_snapshot)
    supplier.seed = False
    supplier.dates = days
    supplier.calls.clear()

    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))

    expected_bao_dates = [days[1:]] if initial_source == "tencent" else [days[-5:]]
    if initial_source == "baostock" and basis_changed:
        expected_bao_dates.append(days[1:])
    assert supplier.calls == [("tencent", days[-6:]), *(("baostock", window) for window in expected_bao_dates)]
    assert result.state == "completed"
    assert result.unresolved_price_cells == int(incomplete)
    snapshot = control.load_state().published_snapshot
    current = reader.read_published_code_window("600001", days[1:], snapshot)
    assert len(current) == 2000
    if incomplete:
        assert current[:-1] == previous[1:]
        assert current[-1].status != "complete"
        assert HistoryDownloadIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).gaps_by_code() == {"600001": days[-1:]}
    else:
        shift = 4.0 if basis_changed else 2.0
        assert all(cell.status == "complete" and cell.qfq.close_price == cell.trade_date.day + shift for cell in current)
        assert HistoryDownloadIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).gap_codes() == frozenset()
        assert HistoryPriceRepair(config.history_root, sqlite_policy=SqlitePolicyFixture()).source("600001", snapshot.sequence) == "baostock"
    assert reader.read_published_code_window("600001", days[:2000], previous_snapshot) == previous


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
    from trader.download.domain.history_download import HistoryDownloadBatch, HistoryDownloadTask

    days = (date(2026, 1, 2), date(2026, 1, 5))
    config = _config(tmp_path, days)
    supplier = RepairHistory(days)
    RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    state = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_published_state()
    HistoryDownloadIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).begin(HistoryDownloadBatch(state.snapshot.sequence + 1, state.source.content_hash, state.calendar.content_hash, state.universe.content_hash, days[-1]), (HistoryDownloadTask("600001", days),))
    supplier.events.clear()
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), repair_only=True)
    assert result.state == "blocked" and result.reason == "history_download_incomplete"
    assert supplier.events == [] and result.snapshot_hash == state.snapshot.content_hash
