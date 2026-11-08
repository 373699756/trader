"""Explicit history fill repairs current facts without reopening old repair receipts."""

from collections import Counter
from dataclasses import replace
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.download.history.infra.history_sync import FakeSupplier, _download
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction
from trader.infra.sqlite_codec import EncodeDate

_DAYS = tuple(date(2025, 12, day) for day in (22, 23, 24, 25, 26, 29, 30, 31)) + (
    date(2026, 1, 2), date(2026, 1, 5),
)
_NOW = datetime.combine(_DAYS[-1], time(20), ZoneInfo("Asia/Shanghai"))


class _SupplementSupplier(FakeSupplier):
    def __init__(self, kind="both"):
        super().__init__(_DAYS)
        self.kind = kind
        self.failure = None
        self.core_code = None
        self.repair_calls = []
        self.tencent_calls = []

    def fetch_tencent_window(self, security, dates):
        self.tencent_calls.append((security.code, dates))
        cells = list(ProjectHistoryCell(cell) for cell in _download(security.code, dates, 0.5).batch.cells)
        if security.code == "600001":
            if self.kind in {"one_price", "both"}:
                cells[0] = replace(
                    cells[0], unadjusted=replace(cells[0].unadjusted, pct_change=None, preclose=None),
                )
            if self.kind in {"inferred", "both"}:
                cells[1] = replace(cells[1], status="tencent_inferred_suspended", unadjusted=None, qfq=None)
            if self.kind == "ordinary_missing":
                raw = cells[0].unadjusted
                cells[0] = replace(cells[0], unadjusted=replace(
                    raw, high_price=raw.close_price + 1.0, preclose=None, pct_change=None,
                ))
        if security.code == self.core_code:
            cells[-1] = replace(cells[-1], status="qfq_missing", qfq=None)
            raise HistorySupplierPendingError(PublishedHistoryWindow(security.code, tuple(cells)))
        return PublishedHistoryWindow(security.code, tuple(cells))

    def fetch_baostock_window(self, security, dates):
        self.repair_calls.append((security.code, dates))
        if self.failure == "supplier" or security.code == self.core_code:
            raise RuntimeError("fixture_supplier_failed")
        cells = list(ProjectHistoryCell(cell) for cell in _download(security.code, dates, 2.0).batch.cells)
        if self.failure == "missing_field":
            cells[0] = replace(cells[0], unadjusted=replace(cells[0].unadjusted, pct_change=None))
        elif self.failure == "drop_known":
            cells[-2] = replace(cells[-2], unadjusted=replace(cells[-2].unadjusted, pct_change=None))
        return PublishedHistoryWindow(security.code, tuple(cells))


def _configuration(tmp_path):
    return HistorySyncConfigurationFixture(
        tmp_path / "history", tmp_path / "reference", sessions=len(_DAYS), reread_sessions=1,
        minimum_free_bytes=0,
    )


def _snapshot(configuration):
    return HistoryControlIndex(
        configuration.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture(),
    ).load_published_state().snapshot


def _cells(configuration, snapshot):
    reader = HistorySnapshotReader(configuration.history_root, sqlite_policy=SqlitePolicyFixture())
    return tuple(row.cell for day in _DAYS for row in reader.read_day(day, snapshot) if row.code == "600001")


def _seed_previous_receipts(configuration, snapshot):
    path = configuration.history_root / "catalog.sqlite3"
    with SqliteSession(path) as connection, SqliteTransaction(connection, immediate=True):
        connection.run(
            "INSERT INTO price_source VALUES('600001',?,'baostock')", (snapshot.sequence,),
        )
        connection.run_many(
            "INSERT INTO price_repair_attempt VALUES('600001',?,'previous','resolved','repaired','baostock')",
            ((EncodeDate(day),) for day in _DAYS),
        )
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).publish_file()
    return _receipts(configuration)


def _receipts(configuration):
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection:
        return tuple(tuple(row) for row in connection.run(
            "SELECT trade_date,batch_id,state,reason,resolved_source FROM price_repair_attempt "
            "WHERE code='600001' ORDER BY trade_date"
        ))


@pytest.mark.parametrize("kind", ("inferred", "one_price", "both"))
def test_fill_repairs_current_auxiliary_facts_despite_resolved_receipts(tmp_path, kind):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier(kind)
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).state == "completed"
    previous = _snapshot(configuration)
    original = _cells(configuration, previous)
    receipts = _seed_previous_receipts(configuration, previous)
    tencent_calls = tuple(supplier.tencent_calls)

    result = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)

    assert result.state == "completed" and not result.needs_retry
    assert result.unresolved_price_cells == result.unresolved_supplement_cells == 0
    assert supplier.repair_calls == [("600001", _DAYS)]
    assert tuple(supplier.tencent_calls) == tencent_calls
    current = _snapshot(configuration)
    assert current.sequence == previous.sequence + 1
    assert _cells(configuration, previous) == original
    assert _receipts(configuration) == receipts
    assert all(cell.status == "complete" for cell in _cells(configuration, current))
    assert all(cell.unadjusted.pct_change is not None for cell in _cells(configuration, current))
    assert all(cell.unadjusted.preclose is not None for cell in _cells(configuration, current))
    assert all(cell.qfq.close_price == cell.trade_date.day + 2.0 for cell in _cells(configuration, current))
    index = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    assert index.gap_codes() == frozenset()
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection:
        assert connection.run("SELECT COUNT(*) FROM download_inferred_suspension").require_one()[0] == 0

    repeated = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)
    assert repeated.state == "already_current"
    assert _snapshot(configuration) == current
    assert supplier.repair_calls == [("600001", _DAYS)]


@pytest.mark.parametrize("failure", ("supplier", "missing_field", "drop_known"))
def test_fill_failed_auxiliary_candidate_remains_retryable_without_core_gap(tmp_path, failure):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier("one_price")
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).state == "completed"
    previous = _snapshot(configuration)
    original = _cells(configuration, previous)
    receipts = _seed_previous_receipts(configuration, previous)
    supplier.failure = failure

    failed = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)

    assert failed.state == "completed" and failed.needs_retry
    assert failed.reason == "history_price_supplements_remaining"
    assert failed.unresolved_price_cells == 0 and failed.unresolved_supplement_cells == 1
    assert supplier.repair_calls == [("600001", _DAYS)]
    assert _cells(configuration, _snapshot(configuration)) == original
    assert _receipts(configuration) == receipts
    index = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    assert index.gap_codes() == frozenset()

    supplier.failure = None
    recovered = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)
    assert recovered.state == "completed" and not recovered.needs_retry
    assert supplier.repair_calls == [("600001", _DAYS), ("600001", _DAYS)]
    assert _cells(configuration, previous) == original


def test_fill_replays_cross_year_auxiliary_candidate_without_second_request(tmp_path, monkeypatch):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier()
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).state == "completed"
    previous = _snapshot(configuration)
    original = _cells(configuration, previous)
    _seed_previous_receipts(configuration, previous)
    replace_code = HistoryPricePartition.replace_unpublished_code

    def interrupted(self, revisions, sequence):
        if revisions[0].trade_date.year == 2026:
            raise KeyboardInterrupt
        return replace_code(self, revisions, sequence)

    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", interrupted)
    cancelled = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)
    assert cancelled.state == "cancelled"
    assert _snapshot(configuration) == previous
    assert _cells(configuration, previous) == original
    assert supplier.repair_calls == [("600001", _DAYS)]
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection:
        assert connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0] == len(_DAYS)

    # Durable candidates also protect work whose sparse task-date inventory is empty.
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection, SqliteTransaction(connection, immediate=True):
        connection.run("DELETE FROM download_task_dates WHERE code='600001'")
    HistoryControlIndex(
        configuration.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture(),
    ).publish_file()
    scoped = RunHistoryFixture(
        configuration, supplier, clock=lambda: _NOW, repair_only=True, repair_codes=frozenset({"600002"}),
    )
    assert scoped.state == "blocked" and scoped.reason == "repair_scope_conflict"
    assert _snapshot(configuration) == previous
    assert supplier.repair_calls == [("600001", _DAYS)]
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection:
        assert connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0] == len(_DAYS)

    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", replace_code)
    recovered = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)
    assert recovered.state == "completed" and not recovered.needs_retry
    assert _snapshot(configuration).sequence == previous.sequence + 1
    assert supplier.repair_calls == [("600001", _DAYS)]
    assert _cells(configuration, previous) == original
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection:
        assert connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0] == 0


def test_fill_discards_supplier_result_when_cancelled_before_candidate_persistence(tmp_path, monkeypatch):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier()
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).state == "completed"
    previous = _snapshot(configuration)
    original = _cells(configuration, previous)
    _seed_previous_receipts(configuration, previous)
    fetch = supplier.fetch_baostock_window
    cancelled = False
    writes = []

    def late_result(security, dates):
        nonlocal cancelled
        candidate = fetch(security, dates)
        cancelled = True
        return candidate

    def unexpected_write(self, revisions, sequence):
        writes.append((revisions, sequence))
        raise AssertionError("cancelled result must not reach annual prices")

    monkeypatch.setattr(supplier, "fetch_baostock_window", late_result)
    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", unexpected_write)
    result = RunHistoryFixture(
        configuration, supplier, clock=lambda: _NOW, repair_only=True, cancel_requested=lambda: cancelled,
    )

    assert result.state == "cancelled"
    assert _snapshot(configuration) == previous
    assert _cells(configuration, previous) == original
    assert supplier.repair_calls == [("600001", _DAYS)]
    assert not writes
    with SqliteSession(configuration.history_root / "catalog.sqlite3") as connection:
        assert connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0] == 0


@pytest.mark.parametrize("core_code", ("600001", "600002"))
def test_fill_core_gap_precedes_auxiliary_code_and_attempts_each_code_once(tmp_path, core_code):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier()
    supplier.core_code = core_code
    first = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW)
    assert first.unresolved_price_cells == 1
    supplier.repair_calls.clear()
    supplier.core_code = None

    repaired = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)

    assert repaired.state == "completed" and not repaired.needs_retry
    expected = ["600001"] if core_code == "600001" else ["600002", "600001"]
    assert [code for code, _ in supplier.repair_calls] == expected
    assert Counter(code for code, _ in supplier.repair_calls) == dict.fromkeys(expected, 1)
    assert all(dates == _DAYS for _, dates in supplier.repair_calls)


def test_fill_does_not_request_ordinary_missing_raw_auxiliary_fields(tmp_path):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier("ordinary_missing")
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).state == "completed"
    previous = _snapshot(configuration)

    unchanged = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)

    assert unchanged.state == "already_current" and not unchanged.needs_retry
    assert not supplier.repair_calls
    assert _snapshot(configuration) == previous
    first = _cells(configuration, previous)[0]
    assert first.unadjusted.pct_change is None and first.unadjusted.preclose is None


@pytest.mark.parametrize("repair_code", ("600001", "600002"))
def test_scoped_fill_repairs_only_selected_code_and_keeps_full_snapshot_population(tmp_path, repair_code):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier("one_price")
    supplier.core_code = "600002"
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).unresolved_price_cells == 1
    previous = _snapshot(configuration)
    reader = HistorySnapshotReader(configuration.history_root, sqlite_policy=SqlitePolicyFixture())
    previous_rows = tuple(reader.read_day(day, previous) for day in _DAYS)
    other_code = "600002" if repair_code == "600001" else "600001"
    supplier.core_code = None
    supplier.repair_calls.clear()
    tencent_calls = tuple(supplier.tencent_calls)

    repaired = RunHistoryFixture(
        configuration, supplier, clock=lambda: _NOW, repair_only=True, repair_codes=frozenset({repair_code}),
    )

    assert repaired.state == "completed" and not repaired.needs_retry
    assert repaired.unresolved_price_cells == repaired.unresolved_supplement_cells == 0
    assert repaired.repair_codes == (repair_code,)
    assert supplier.repair_calls == [(repair_code, _DAYS)]
    assert tuple(supplier.tencent_calls) == tencent_calls
    current = _snapshot(configuration)
    assert current.sequence == previous.sequence + 1
    assert current.universe_hash == previous.universe_hash
    for day, before in zip(_DAYS, previous_rows, strict=True):
        assert reader.read_day(day, previous) == before
        after = reader.read_day(day, current)
        assert {row.code for row in after} == {row.code for row in before}
        assert next(row.cell for row in after if row.code == other_code) == next(
            row.cell for row in before if row.code == other_code
        )
    index = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    assert index.gap_codes() == (frozenset({"600002"}) if repair_code == "600001" else frozenset())

    repeated = RunHistoryFixture(
        configuration, supplier, clock=lambda: _NOW, repair_only=True, repair_codes=frozenset({repair_code}),
    )
    assert repeated.state == "already_current" and not repeated.needs_retry
    assert repeated.repair_codes == (repair_code,)
    assert _snapshot(configuration) == current
    assert supplier.repair_calls == [(repair_code, _DAYS)]

    completed = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)
    assert completed.state == "completed" and not completed.needs_retry
    assert completed.repair_codes == ()
    assert supplier.repair_calls == [(repair_code, _DAYS), (other_code, _DAYS)]


@pytest.mark.parametrize("kind", ("inferred", "one_price"))
def test_fill_adds_auxiliary_dates_when_resuming_old_ordinary_pending_batch(tmp_path, monkeypatch, kind):
    configuration = _configuration(tmp_path)
    supplier = _SupplementSupplier(kind)
    supplier.core_code = "600002"
    assert RunHistoryFixture(configuration, supplier, clock=lambda: _NOW).unresolved_price_cells == 1
    previous = _snapshot(configuration)
    original = _cells(configuration, previous)
    reserve = HistoryPriceRepair.reserve

    def interrupted(self, code, dates, batch, *, explicit):
        raise KeyboardInterrupt

    monkeypatch.setattr(HistoryPriceRepair, "reserve", interrupted)
    interrupted_result = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW)
    assert interrupted_result.state == "cancelled"
    index = HistoryDownloadIndex(configuration.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    pending = index.pending(previous.sequence)
    assert pending is not None and pending.sequence == previous.sequence + 1
    tasks = {task.code: task for task in index.tasks()}
    assert tasks["600001"].dates == ()
    assert tasks["600002"].dates and tasks["600002"].tencent_done
    core_task = tasks["600002"]
    tencent_calls = tuple(supplier.tencent_calls)
    supplier.repair_calls.clear()
    supplier.core_code = None

    scoped = RunHistoryFixture(
        configuration, supplier, clock=lambda: _NOW, repair_only=True, repair_codes=frozenset({"600001"}),
    )
    assert scoped.state == "blocked" and scoped.reason == "repair_scope_conflict"
    assert scoped.repair_codes == ("600001",)
    assert index.pending(previous.sequence) == pending
    assert {task.code: task for task in index.tasks()} == tasks
    assert _snapshot(configuration) == previous
    assert not supplier.repair_calls

    interrupted_fill = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)
    assert interrupted_fill.state == "cancelled"
    augmented = {task.code: task for task in index.tasks()}
    assert augmented["600001"].dates and augmented["600001"].tencent_done
    assert augmented["600002"] == core_task
    monkeypatch.setattr(HistoryPriceRepair, "reserve", reserve)

    repaired = RunHistoryFixture(configuration, supplier, clock=lambda: _NOW, repair_only=True)

    assert repaired.state == "completed" and not repaired.needs_retry
    assert repaired.unresolved_price_cells == repaired.unresolved_supplement_cells == 0
    assert supplier.repair_calls == [("600002", _DAYS), ("600001", _DAYS)]
    assert tuple(supplier.tencent_calls) == tencent_calls
    assert _snapshot(configuration).sequence == pending.sequence
    assert _cells(configuration, previous) == original
    repaired_tasks = {task.code: task for task in index.tasks()}
    assert repaired_tasks["600002"].dates == core_task.dates
    assert repaired_tasks["600002"].attempts == core_task.attempts
    assert repaired_tasks["600001"].dates
