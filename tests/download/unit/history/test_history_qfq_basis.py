"""A changed Tencent basis replaces one whole published window at a new sequence."""

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tests.helpers.download.history.infra.history_sync import FakeSupplier
from tests.helpers.download.history_fixtures import RunHistoryFixture
from tests.helpers.download.configuration import HistorySyncConfigurationFixture
from tests.helpers.shared.configuration import SqlitePolicyFixture
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_write_tuning import HistoryWriteTuning
from trader.infra.sqlite_connection import SqliteConnectionOptions, SqliteSession


_DAYS = (date(2025, 12, 29), date(2025, 12, 30), date(2025, 12, 31), date(2026, 1, 2), date(2026, 1, 5))


def _now(day):
    return datetime.combine(day, datetime.min.time(), ZoneInfo("Asia/Shanghai")).replace(hour=20)


class _BasisSupplier(FakeSupplier):
    def __init__(self, days):
        super().__init__(days)
        self.seed_cutoff = days[-1]
        self.rebased = False
        self.failure = None
        self.requests = []
        self.suspended = frozenset()

    def load_context(self, as_of, sessions, **kwargs):
        context = super().load_context(as_of, sessions, **kwargs)
        self.expected = context.calendar.open_dates
        return replace(context, universe=context.universe[:1], st_evidence=context.st_evidence[:1])

    def fetch_tencent_window(self, security, dates):
        self.requests.append(dates)
        full_refresh = self.rebased and dates == self.expected
        if full_refresh and self.failure == "cancel":
            raise KeyboardInterrupt
        if full_refresh and self.failure == "transport":
            raise RuntimeError("supplier_query_failed")
        cells = []
        for day in dates:
            raw_price = 50.0 if self.rebased and day > self.seed_cutoff else 100.0
            qfq_price = 50.0 if self.rebased else 100.0
            suspended = day in self.suspended
            raw = PublishedHistorySide(
                security.code, day, "unadjusted", raw_price, raw_price, raw_price, raw_price,
                0.0 if suspended else 10000.0, 0.0 if suspended else 1000000.0,
                None, None, None, "suspended" if suspended else "trading",
            )
            qfq = replace(raw, adjustment="qfq", open_price=qfq_price, high_price=qfq_price, low_price=qfq_price, close_price=qfq_price)
            cells.append(PublishedHistoryCell(security.code, day, "supplier_marked_suspended" if suspended else "complete", raw, qfq))
        if full_refresh and self.failure == "missing":
            cells[0] = replace(cells[0], status="qfq_missing", qfq=None)
        if full_refresh and self.failure == "short":
            cells = cells[1:]
        if full_refresh and self.failure == "raw":
            cells[0] = replace(cells[0], unadjusted=replace(cells[0].unadjusted, close_price=101.0))
        window = PublishedHistoryWindow(security.code, tuple(cells))
        if full_refresh and self.failure == "pending":
            raise HistorySupplierPendingError(window, "tencent_qfq_invalid_price", dates[:1])
        return window

    def fetch_baostock_window(self, security, dates):
        raise RuntimeError("fixture_baostock_unavailable")


def _seed(tmp_path, supplier=None):
    days = _DAYS[:-1] if supplier is None else supplier.dates
    supplier = supplier or _BasisSupplier(days)
    config = HistorySyncConfigurationFixture(
        tmp_path / "history", tmp_path / "reference", sessions=len(days), reread_sessions=1, minimum_free_bytes=0,
    )
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(days[-1])).state == "completed"
    policy = SqlitePolicyFixture()
    control = HistoryControlIndex(config.history_root / "catalog.sqlite3", sqlite_policy=policy)
    snapshot = control.load_state().published_snapshot
    reader = HistorySnapshotReader(config.history_root, sqlite_policy=policy)
    previous = reader.read_published_code_window("600001", days, snapshot)
    supplier.requests.clear()
    return supplier, config, control, reader, snapshot, previous


def test_basis_change_automatically_replaces_the_whole_current_window_and_keeps_old_snapshot(tmp_path):
    supplier, config, control, reader, old_snapshot, old_cells = _seed(tmp_path)
    supplier.dates, supplier.rebased = _DAYS, True
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(_DAYS[-1]))
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.requests == [_DAYS[-2:], _DAYS[1:]]
    current = control.load_state().published_snapshot
    assert current.sequence == old_snapshot.sequence + 1
    cells = reader.read_published_code_window("600001", _DAYS[1:], current)
    assert [cell.qfq.close_price for cell in cells] == [50.0] * 4
    assert reader.read_published_code_window("600001", _DAYS[:-1], old_snapshot) == old_cells
    # The oldest retained physical date lies outside this publication's calendar.
    assert reader.read_day(_DAYS[0], current)[0].cell == reader.read_day(_DAYS[0], old_snapshot)[0].cell
    assert HistoryPriceRepair(config.history_root, sqlite_policy=SqlitePolicyFixture()).source("600001", current.sequence) == "tencent"


def test_unchanged_basis_keeps_incremental_requests_bounded(tmp_path):
    supplier, config, _control, _reader, _old_snapshot, _old_cells = _seed(tmp_path)
    supplier.dates = _DAYS
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(_DAYS[-1]))
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.requests == [_DAYS[-2:]]


@pytest.mark.parametrize("failure", ("transport", "pending", "missing", "short", "raw"))
def test_failed_whole_window_never_commits_the_already_fetched_incompatible_tail(tmp_path, failure):
    supplier, config, control, reader, old_snapshot, old_cells = _seed(tmp_path)
    supplier.dates, supplier.rebased, supplier.failure = _DAYS, True, failure
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(_DAYS[-1]))
    assert result.needs_retry
    assert supplier.requests == [_DAYS[-2:], _DAYS[1:]]
    snapshot = control.load_state().published_snapshot
    assert reader.read_published_code_window("600001", _DAYS[:-1], old_snapshot) == old_cells
    if snapshot.data_cutoff == _DAYS[-1]:
        latest = reader.read_day(_DAYS[-1], snapshot)[0].cell
        assert latest.status != "complete" and latest.qfq is None
    else:
        assert result.state == "failed" and snapshot == old_snapshot
    with SqliteSession(config.history_root / "catalog.sqlite3", SqliteConnectionOptions(read_only=True), sqlite_policy=SqlitePolicyFixture()) as connection:
        assert connection.run("SELECT COUNT(*) FROM price_replacement").require_one()[0] == 0


def test_cancellation_before_whole_candidate_keeps_the_published_sequence_and_tail_pending(tmp_path):
    supplier, config, control, reader, old_snapshot, old_cells = _seed(tmp_path)
    supplier.dates, supplier.rebased, supplier.failure = _DAYS, True, "cancel"
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(_DAYS[-1]))
    assert result.state == "cancelled"
    assert control.load_state().published_snapshot == old_snapshot
    assert reader.read_published_code_window("600001", _DAYS[:-1], old_snapshot) == old_cells
    tasks = HistoryDownloadIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).tasks()
    assert len(tasks) == 1 and not tasks[0].tencent_done


def test_cross_year_replacement_replays_after_interruption_without_a_second_supplier_request(tmp_path, monkeypatch):
    supplier, config, control, reader, old_snapshot, old_cells = _seed(tmp_path)
    supplier.dates, supplier.rebased = _DAYS, True
    original = HistoryPricePartition.replace_unpublished_code

    def interrupt_second_year(partition, revisions, sequence):
        if revisions[0].trade_date.year == 2026:
            raise RuntimeError("fixture_write_interrupted")
        return original(partition, revisions, sequence)

    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", interrupt_second_year)
    assert RunHistoryFixture(config, supplier, clock=lambda: _now(_DAYS[-1])).state == "failed"
    assert control.load_state().published_snapshot == old_snapshot
    tasks = HistoryDownloadIndex(config.history_root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).tasks()
    assert not tasks[0].tencent_done
    supplier.requests.clear()
    monkeypatch.setattr(HistoryPricePartition, "replace_unpublished_code", original)
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(_DAYS[-1]))
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.requests == []
    snapshot = control.load_state().published_snapshot
    assert [cell.qfq.close_price for cell in reader.read_published_code_window("600001", _DAYS[1:], snapshot)] == [50.0] * 4
    assert reader.read_published_code_window("600001", _DAYS[:-1], old_snapshot) == old_cells


def test_full_window_fallback_capacity_is_checked_before_dispatch(tmp_path):
    supplier, config, control, _reader, old_snapshot, _old_cells = _seed(tmp_path)
    supplier.dates, supplier.rebased = _DAYS, True
    result = RunHistoryFixture(
        config, supplier, clock=lambda: _now(_DAYS[-1]), write_tuning=HistoryWriteTuning(1, 2, 1, 2),
    )
    assert result.state == "failed" and supplier.requests == []
    assert control.load_state().published_snapshot == old_snapshot


def test_suspended_tail_without_a_price_anchor_uses_the_whole_window(tmp_path):
    days = tuple(date(2026, 10, 1) + timedelta(days=number) for number in range(10))
    supplier = _BasisSupplier(days)
    supplier.suspended = frozenset(days[-6:])
    supplier, config, control, reader, _old_snapshot, _old_cells = _seed(tmp_path, supplier)
    extra = days[-1] + timedelta(days=1)
    supplier.dates, supplier.rebased = (*days, extra), True
    result = RunHistoryFixture(config, supplier, clock=lambda: _now(extra))
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert supplier.requests == [(days[-1], extra), (*days[1:], extra)]
    snapshot = control.load_state().published_snapshot
    cells = reader.read_published_code_window("600001", (*days[1:], extra), snapshot)
    assert [cell.qfq.close_price for cell in cells if cell.status == "complete"] == [50.0] * 4
