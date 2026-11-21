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


@pytest.mark.parametrize("failure_mode", ("none", "read", "write", "supplier"))
def test_history_reports_obtained_before_replacement_and_stops_on_database_failure(tmp_path, monkeypatch, failure_mode):
    import sqlite3

    from tests.helpers.download.history.infra.history_sync import ProgressRecorder
    from trader.download.infra import history_sync_io

    bao_calls = []

    class AllGapped(RepairHistory):
        def fetch_tencent_window(self, security, dates):
            window = super().fetch_tencent_window(security, dates)
            partial = replace(window, cells=(replace(window.cells[0], status="qfq_missing", qfq=None), *window.cells[1:]))
            raise HistorySupplierPendingError(partial, "tencent_qfq_invalid_price", dates[:1])

        def fetch_baostock_window(self, security, dates):
            bao_calls.append(security.code)
            if failure_mode == "supplier" and security.code == "600001":
                raise RuntimeError("supplier_query_failed")
            return super().fetch_baostock_window(security, dates)

    days = (date(2026, 1, 2), date(2026, 1, 5))
    supplier = AllGapped(days)
    config = _config(tmp_path, days)
    progress = ProgressRecorder()
    original = HistoryPriceRepair.replace
    entered = []

    def replace_prices(self, security, dates, incoming, prices, sequence, **options):
        reports = [item for item in progress.values if item.stage == "baostock_repair" and item.current_item == security.code]
        assert len(reports) == 1
        assert reports[0].state == "obtained"
        assert reports[0].repair_summary is None
        assert reports[0].completed_units == (1 if security.code == "600001" else 2)
        assert reports[0].total_units == 2
        assert tuple(cell.trade_date for cell in incoming.cells) == dates
        entered.append(security.code)
        if failure_mode == "write":
            raise RuntimeError("fixture_write_failed") from sqlite3.OperationalError("fixture_database_failure")
        return original(self, security, dates, incoming, prices, sequence, **options)

    monkeypatch.setattr(HistoryPriceRepair, "replace", replace_prices)
    if failure_mode == "read":
        def fail_read(self, code, requested_dates):
            assert code == "600001" and requested_dates == days
            raise RuntimeError("fixture_read_failed") from sqlite3.OperationalError("fixture_database_failure")

        monkeypatch.setattr(HistoryPriceRepair, "source", lambda *args: "baostock")
        monkeypatch.setattr(history_sync_io._HistoryPriceBatch, "read_prices", fail_read)
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]), progress=progress)
    reports = [item for item in progress.values if item.stage == "baostock_repair"]
    stock_reports = [item for item in reports if item.current_item == "600001"]
    if failure_mode in {"read", "write"}:
        assert result.state == "failed"
        assert bao_calls == ([] if failure_mode == "read" else ["600001"])
        assert entered == ([] if failure_mode == "read" else ["600001"])
        assert sum(item.state == "obtained" for item in stock_reports) == int(failure_mode == "write")
        assert not any(item.repair_summary is not None and item.repair_summary.repaired for item in reports)
        with SqliteSession(config.history_root / "catalog.sqlite3") as connection:
            assert [tuple(row) for row in connection.run(
                "SELECT code,state,resolved_source FROM price_repair_attempt ORDER BY code",
            )] == [("600001", "requested", None)]
        return
    assert result.state == "completed" and result.unresolved_price_cells == int(failure_mode == "supplier")
    assert bao_calls == ["600001", "600002"]
    assert entered == (["600002"] if failure_mode == "supplier" else ["600001", "600002"])
    assert [item.state for item in stock_reports] == (["failed"] if failure_mode == "supplier" else ["obtained"])
    if failure_mode == "supplier":
        assert stock_reports[0].failure_reason == "supplier_query_failed"
    summary = reports[-1].repair_summary
    assert reports[-1].state == "completed" and summary is not None
    assert (summary.attempted, summary.repaired, summary.skipped) == (2, 1 if failure_mode == "supplier" else 2, 0)


@pytest.mark.parametrize("field", ("amount", "volume", "close_price"))
def test_history_conflicting_bao_candidate_keeps_old_prices_and_repairs_next_stock(tmp_path, field):
    from tests.helpers.download.history.infra.history_sync import ProgressRecorder

    calls = []
    prior = {}

    class ConflictingRepair(RepairHistory):
        def fetch_tencent_window(self, security, dates):
            window = PublishedHistoryWindow(security.code, tuple(
                ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells
            ))
            partial = replace(window, cells=(replace(window.cells[0], status="qfq_missing", qfq=None), *window.cells[1:]))
            prior[security.code] = partial
            raise HistorySupplierPendingError(partial, "tencent_qfq_invalid_price", dates[:1])

        def fetch_baostock_window(self, security, dates):
            calls.append(security.code)
            window = super().fetch_baostock_window(security, dates)
            if security.code == "600001":
                cell = window.cells[-1]
                raw = replace(cell.unadjusted, **{field: getattr(cell.unadjusted, field) + 100000.0})
                window = replace(window, cells=(*window.cells[:-1], replace(cell, unadjusted=raw)))
            return window

    days = (date(2026, 1, 2), date(2026, 1, 5))
    config = _config(tmp_path, days)
    progress = ProgressRecorder()
    result = RunHistoryFixture(config, ConflictingRepair(days), clock=lambda: _now(days[-1]), progress=progress)
    assert result.state == "completed" and result.unresolved_price_cells == 1
    assert calls == ["600001", "600002"]
    reports = [item for item in progress.values if item.stage == "baostock_repair"]
    assert [(item.current_item, item.failure_reason) for item in reports if item.state == "failed"] == [
        ("600001", "history_tail_raw_price_conflict"),
    ]
    assert reports[-1].repair_summary.repaired == 1
    path = config.history_root / "catalog.sqlite3"
    with SqliteSession(path) as connection:
        assert [tuple(row) for row in connection.run(
            "SELECT code,state,resolved_source FROM price_repair_attempt ORDER BY code",
        )] == [("600001", "failed", None), ("600002", "resolved", "baostock")]
        assert connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0] == 0
    snapshot = HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    cells = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_published_code_window(
        "600001", days, snapshot,
    )
    assert tuple(ProjectHistoryCell(cell) for cell in cells) == prior["600001"].cells


def test_history_does_not_report_obtained_for_incomplete_supplier_candidate(tmp_path, monkeypatch):
    from tests.helpers.download.history.infra.history_sync import ProgressRecorder

    class IncompleteRepair(RepairHistory):
        def fetch_baostock_window(self, security, dates):
            window = super().fetch_baostock_window(security, dates)
            return PublishedHistoryWindow(security.code, (
                replace(window.cells[0], status="qfq_missing", qfq=None), *window.cells[1:],
            ))

    def replace_prices(*args, **kwargs):
        raise AssertionError("incomplete_candidate_must_not_enter_replacement")

    days = (date(2026, 1, 2), date(2026, 1, 5))
    config = _config(tmp_path, days)
    progress = ProgressRecorder()
    monkeypatch.setattr(HistoryPriceRepair, "replace", replace_prices)
    result = RunHistoryFixture(config, IncompleteRepair(days), clock=lambda: _now(days[-1]), progress=progress)
    assert result.state == "completed" and result.unresolved_price_cells == 1
    reports = [item for item in progress.values if item.stage == "baostock_repair"]
    stock_reports = [item for item in reports if item.current_item == "600001"]
    assert [item.state for item in stock_reports] == ["failed"]
    assert stock_reports[0].failure_reason == "price_replacement_window_incomplete"
    assert reports[-1].repair_summary is not None and reports[-1].repair_summary.repaired == 0


def test_failed_history_gap_is_retried_in_a_new_run_and_old_snapshot_is_immutable(tmp_path):
    days = (date(2026, 1, 2), date(2026, 1, 5))
    supplier = RepairHistory(days)
    supplier.fail_repair = True
    config = _config(tmp_path, days)
    first = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert first.unresolved_price_cells == 1
    snapshot = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_state().published_snapshot
    previous = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(days[0], snapshot)
    supplier.fail_repair = False
    supplier.events.clear()
    second = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert second.state == "completed" and second.unresolved_price_cells == 0
    assert supplier.events == ["baostock"]
    assert HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture()).read_day(days[0], snapshot) == previous
    with SqliteSession(config.history_root / "catalog.sqlite3") as connection:
        assert [tuple(row) for row in connection.run("SELECT state,resolved_source FROM price_repair_attempt")] == [("resolved", "baostock")]
    supplier.events.clear()
    third = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert third.state == "already_current" and third.unresolved_price_cells == 0
    assert supplier.events == []


def test_same_day_history_retries_timed_out_bao_without_repeating_tencent_or_changing_old_snapshot(tmp_path):
    class TimeoutHistory(RepairHistory):
        timeout = True

        def fetch_baostock_window(self, security, dates):
            if self.timeout:
                self.events.append("baostock")
                raise RuntimeError("supplier_price_total_timeout")
            return super().fetch_baostock_window(security, dates)

    days = (date(2026, 1, 2), date(2026, 1, 5))
    supplier = TimeoutHistory(days)
    config = _config(tmp_path, days)
    first = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert first.state == "completed" and first.unresolved_price_cells == 1
    assert supplier.events == ["tencent", "tencent", "baostock"]
    path = config.history_root / "catalog.sqlite3"
    control = HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture())
    previous = control.load_state().published_snapshot
    reader = HistorySnapshotReader(config.history_root, sqlite_policy=SqlitePolicyFixture())
    old_rows = reader.read_day(days[0], previous)
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    # Tencent's same-day qfq cooldown remains in force; Bao alone must be reachable.
    assert index.gaps_by_code(days) == {}
    assert index.gaps_by_code(days, retryable_only=False) == {"600001": days[:1]}
    with SqliteSession(path) as connection:
        receipt = connection.run("SELECT batch_id,state,reason,resolved_source FROM price_repair_attempt").require_one()
        first_batch = receipt[0]
        assert tuple(receipt[1:]) == ("failed", "supplier_price_total_timeout", None)
    supplier.timeout = False
    supplier.events.clear()
    second = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert second.state == "completed" and second.unresolved_price_cells == 0
    assert supplier.events == ["baostock"]
    current = control.load_state().published_snapshot
    assert current.sequence > previous.sequence
    assert reader.read_day(days[0], previous) == old_rows
    current_rows = reader.read_day(days[0], current)
    assert next(row.cell for row in current_rows if row.code == "600001").qfq.close_price == days[0].day + 2.0
    assert next(row.cell for row in current_rows if row.code == "600002") == next(
        row.cell for row in old_rows if row.code == "600002"
    )
    with SqliteSession(path) as connection:
        receipt = connection.run("SELECT batch_id,state,resolved_source FROM price_repair_attempt").require_one()
        assert receipt[0] != first_batch and tuple(receipt[1:]) == ("resolved", "baostock")
    supplier.events.clear()
    third = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
    assert third.state == "already_current" and third.unresolved_price_cells == 0
    assert supplier.events == []
    assert control.load_state().published_snapshot == current


def test_rolling_calendar_ignores_old_gap_and_repeat_makes_no_supplier_requests(tmp_path):
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
    repeated = RunHistoryFixture(config, current_supplier, clock=lambda: _now(days[-1]))
    assert repeated.state == "already_current" and repeated.unresolved_price_cells == 0
    assert repeated.reason is None and repeated.price_gap_summary.cells == 0
    assert current_supplier.events == [] and current_supplier.calls == []
    assert HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture()).gaps_by_code() == {"600001": days[:1]}
    assert reader.read_day(days[0], previous) == old_cells
    with SqliteSession(path) as connection:
        assert [row[0] for row in connection.run("SELECT state FROM price_repair_attempt")] == ["failed"]


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
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
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
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1]))
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


def test_market_second_phase_retries_previous_run_failure_then_reuses_the_success(tmp_path):
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
    bao.fail = False
    events.clear()
    second = updater.execute(_now(DAYS[-1]))
    assert second.pending_codes == 0 and second.failure_reason is None
    assert events == ["tencent", "baostock"]
    events.clear()
    third = updater.execute(_now(DAYS[-1]))
    assert third.skipped_codes == 1 and third.pending_codes == 0 and events == []


def test_same_day_market_retries_timed_out_bao_and_reuses_successfully_published_stocks(tmp_path):
    events = []

    class Tencent(Supplier):
        def fetch_window(self, security, dates):
            events.append(("tencent", security.code))
            window = _window(security.code, dates)
            if security.code == "600001":
                partial = replace(window, cells=(replace(window.cells[0], status="qfq_missing", qfq=None), *window.cells[1:]))
                raise HistorySupplierPendingError(partial, "tencent_qfq_invalid_price", dates[:1])
            return window

    class Bao:
        timeout = True

        def fetch_baostock_window(self, security, dates):
            events.append(("baostock", security.code))
            if self.timeout:
                raise RuntimeError("supplier_price_total_timeout")
            window = _window(security.code, dates)
            return replace(window, cells=tuple(
                replace(cell, qfq=replace(cell.qfq, open_price=20.0, high_price=20.2, low_price=19.8, close_price=20.0))
                for cell in window.cells
            ))

    bao = Bao()
    updater = replace(_updater(tmp_path, Tencent(codes=("600001", "600002"))), repair_supplier=bao)
    first = updater.execute(_now(DAYS[-1]))
    assert first.completed_codes == 1 and first.pending_codes == 1
    assert events.count(("tencent", "600001")) == events.count(("tencent", "600002")) == 1
    assert events[-1] == ("baostock", "600001")
    retained = updater.v2.read_code("600002")
    path = tmp_path / "daily_window.sqlite3"
    with SqliteSession(path) as connection:
        first_batch = connection.run("SELECT batch_id FROM price_repair_attempt").require_one()[0]
    bao.timeout = False
    events.clear()
    second = updater.execute(_now(DAYS[-1]))
    assert second.pending_codes == 0 and second.failure_reason is None
    assert events == [("tencent", "600001"), ("baostock", "600001")]
    assert updater.v2.read_code("600002") == retained
    assert all(cell.qfq.close_price == 20.0 for cell in updater.v2.read_code("600001").cells)
    with SqliteSession(path) as connection:
        receipt = connection.run("SELECT batch_id,state,resolved_source FROM price_repair_attempt").require_one()
        assert receipt[0] != first_batch and tuple(receipt[1:]) == ("resolved", "baostock")
    events.clear()
    third = updater.execute(_now(DAYS[-1]))
    assert third.pending_codes == 0 and third.skipped_codes == 2 and third.changed_files == ()
    assert events == [] and updater.v2.read_code("600002") == retained



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

    class Unavailable(Supplier):
        def fetch_window(self, security, dates):
            raise RuntimeError("supplier_query_failed")

    class Bao:
        def fetch_baostock_window(self, security, dates):
            return _window(security.code, dates, price=12.0)

    result = replace(updater, supplier=Unavailable(), repair_supplier=Bao()).execute(_now(DAYS[-1]))
    assert result.pending_codes == 1
    assert updater.v2.read_code("600001") == previous
    assert not updater.v2.completed(DAYS[-1], "600001", updater.v2.source_identity("600001"))
