"""Auxiliary inferred-suspension review has its own bounded, resumable receipts."""

import sqlite3
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.configuration import SqlitePolicyFixture
from tests.helpers.history import _PublishedSupplier
from tests.unit.infra.research.test_history_sync import NOW, FakeSupplier, _configuration, _download
from trader.download.domain.history_revision import HistoryRevision
from trader.download.domain.price_review import PriceReviewBudgetExhausted, PriceReviewConfiguration
from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
from trader.download.infra import history_sqlite_read_connection, history_sync_runner
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_direct_prices import HistoryDirectPrices
from trader.download.infra.history_download_index import (
    HistoryDownloadBatch,
    HistoryDownloadCompletion,
    HistoryDownloadIndex,
    HistoryDownloadTask,
)
from trader.download.infra.history_inferred_review import HistoryInferredReviewIndex
from trader.download.infra.history_price_repair import HistoryPriceRepair
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.history_snapshot_inspection import (
    LoadPublishedHistorySnapshot,
    VerifyPublishedHistorySnapshot,
)
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_sqlite_activity import HistorySqliteActivity
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.download.infra.tencent_request_budget import TencentRequestBudget
from trader.infra.sqlite_codec import DecodeDate

DAYS = tuple(date(2026, 9, 1) + timedelta(days=offset) for offset in range(10))


class _BudgetSupplier(_PublishedSupplier):
    def fetch_tencent_window(self, security, dates, *, request_budget=None):
        return self.supplier.fetch_tencent_window(security, dates, request_budget=request_budget)


class _InferredSupplier(FakeSupplier):
    def __init__(self, *, inferred=True, exhausted=False):
        super().__init__(DAYS)
        self.inferred = inferred
        self.exhausted = exhausted
        self.review_calls = []

    def fetch_tencent_window(self, security, dates, *, request_budget=None):
        if request_budget is not None:
            if self.exhausted:
                raise PriceReviewBudgetExhausted("request_limit")
            request_budget.reserve_request(15.0)
            request_budget.reserve_request(15.0)
            self.review_calls.append((security.code, dates))
        window = super().fetch_tencent_window(security, dates)
        return replace(window, cells=tuple(
            replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
            if self.inferred and security.code == "600001" and cell.trade_date == DAYS[0] else cell
            for cell in window.cells
        ))


def _run(root, supplier, *, budget=None):
    configuration = replace(_configuration(root), sessions=len(DAYS))
    return RunHistorySync(
        configuration,
        _BudgetSupplier(supplier, configuration.reference_root / "company_st.sqlite3"),
        clock=lambda: NOW,
        price_review_budget=budget,
        sqlite_policy=SqlitePolicyFixture(),
    )


def _budget():
    return TencentRequestBudget(monotonic=lambda: 0.0)


def _legacy_metadata(root):
    # The old released format already contains inferred facts but no inventory.
    with sqlite3.connect(root / "catalog.sqlite3") as connection:
        connection.execute("DELETE FROM download_inferred_suspension")
    HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).publish_file()


def _snapshot(root):
    return HistoryControlIndex(root / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).load_published_state().snapshot


def _assert_public_snapshot(root, snapshot):
    history = LoadPublishedHistorySnapshot(root, sqlite_policy=SqlitePolicyFixture())
    pinned = LoadPublishedHistorySnapshot(root, sequence=snapshot.sequence, sqlite_policy=SqlitePolicyFixture())
    assert history.snapshot == pinned.snapshot == snapshot
    VerifyPublishedHistorySnapshot(history, sqlite_policy=SqlitePolicyFixture())
    reader = HistorySnapshotReader(root, sqlite_policy=SqlitePolicyFixture())
    rows = reader.read_code_window("600001", DAYS, pinned.snapshot)
    assert len(rows) == len(DAYS)
    assert rows[0].cell.status == "tencent_inferred_suspended"
    assert all(row.cell.status == "complete" for row in rows[1:])


def test_same_cutoff_review_adds_a_new_sequence_and_preserves_old_published_fact(tmp_path):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    supplier = _InferredSupplier(inferred=False)
    result = _run(tmp_path, supplier, budget=_budget())
    after = _snapshot(tmp_path)
    partition = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())

    assert result.state == "completed", result.reason
    assert after.sequence == before.sequence + 1
    assert after.data_cutoff == before.data_cutoff
    assert partition.read_price_keys((("600001", DAYS[0]),), before.sequence)[0].cell.status == "tencent_inferred_suspended"
    assert partition.read_price_keys((("600001", DAYS[0]),), after.sequence)[0].cell.status == "complete"
    assert result.unresolved_price_cells == 0
    assert result.price_review.physical_requests == 2
    assert result.price_review.remaining_inferred_codes == 0
    assert result.price_review.discovery_pending
    assert len(supplier.review_calls) == 1


def test_unchanged_review_keeps_snapshot_and_does_not_reset_five_session_age(tmp_path):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    supplier = _InferredSupplier()
    first = _run(tmp_path, supplier, budget=_budget())
    second_supplier = _InferredSupplier()
    second = _run(tmp_path, second_supplier, budget=_budget())

    assert first.state == second.state == "already_current"
    assert _snapshot(tmp_path) == before
    assert len(supplier.review_calls) == 1
    assert second_supplier.review_calls == []
    assert second.price_review.physical_requests == 0
    assert second.price_review.remaining_inferred_codes == 1
    assert HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).pending(before.sequence) is None


def test_budget_exhaustion_defers_without_supplier_failure_gap_or_false_review_receipt(tmp_path):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    result = _run(tmp_path, _InferredSupplier(exhausted=True), budget=_budget())
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())

    assert result.state == "already_current"
    assert result.reason is None
    assert _snapshot(tmp_path) == before
    assert index.gap_codes() == frozenset()
    assert all(task.failure is None for task in index.tasks())
    supplier = _InferredSupplier(inferred=False)
    assert _run(tmp_path, supplier, budget=_budget()).state == "completed"
    assert len(supplier.review_calls) == 1


def test_legacy_discovery_rotates_eight_exact_stock_windows_and_resumes(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    partition = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    partition.initialize()
    universe = tuple(replace(FakeSupplier(DAYS).load_context(DAYS[-1], len(DAYS)).universe[0], code=f"600{index:03d}") for index in range(1, 10))
    context = FakeSupplier(DAYS).load_context(DAYS[-1], len(DAYS), universe=universe)
    partition.save_revisions(tuple(sorted((
        HistoryRevision(1, "main", replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                        if security.code == "600009" and cell.trade_date == DAYS[0] else cell, None, None, None)
        for security in universe for cell in _download(security.code, DAYS).batch.cells
    ), key=lambda revision: (revision.trade_date, revision.code))))
    prices = HistoryDirectPrices(tmp_path, 1, None, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=SqlitePolicyFixture())
    index = HistoryInferredReviewIndex(path, sqlite_policy=SqlitePolicyFixture())
    index.initialize()
    first_budget = _budget()
    assert index.plan(context, prices, 1, PriceReviewConfiguration(), first_budget, lambda: False) == ()
    assert first_budget.reviewed_codes == tuple(security.code for security in universe[:8])
    second_budget = _budget()
    tasks = index.plan(context, prices, 1, PriceReviewConfiguration(), second_budget, lambda: False)
    assert second_budget.reviewed_codes[0] == "600009"
    assert len(second_budget.reviewed_codes) == 8
    assert [(task.code, task.inferred_dates) for task in tasks] == [("600009", (DAYS[0],))]


def test_discovery_deadline_leaves_cursor_for_next_command(tmp_path, monkeypatch):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    context = FakeSupplier(DAYS).load_context(DAYS[-1], len(DAYS))
    prices = HistoryDirectPrices(tmp_path, 1, None, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=SqlitePolicyFixture())
    index = HistoryInferredReviewIndex(path, sqlite_policy=SqlitePolicyFixture())
    index.initialize()

    def expired(*_args):
        raise PriceReviewBudgetExhausted("deadline")

    monkeypatch.setattr(prices, "read_for_review", expired)
    budget = _budget()
    assert index.plan(context, prices, 1, PriceReviewConfiguration(), budget, lambda: False) == ()
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT after_code FROM download_inferred_cursor").fetchall() == []
    assert budget.reviewed_codes == ("600001",)


def test_auxiliary_revision_fence_rejects_published_or_unknown_sequence(tmp_path):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    for sequence in (1, 2):
        with pytest.raises(RuntimeError, match="history_download_identity_conflict"):
            index.require_unpublished(sequence)


def test_network_review_failure_keeps_due_age_and_next_same_day_can_retry(tmp_path, caplog):
    class FailedReview(_InferredSupplier):
        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            if request_budget is not None:
                request_budget.reserve_request(15.0)
                raise OSError("fixture_review_offline")
            return super().fetch_tencent_window(security, dates)

    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    failed = _run(tmp_path, FailedReview(), budget=_budget())
    assert failed.state == "already_current"
    assert _snapshot(tmp_path) == before
    assert failed.unresolved_price_cells == 0
    assert "history_inferred_review_failed" in caplog.text
    supplier = _InferredSupplier(inferred=False)
    assert _run(tmp_path, supplier, budget=_budget()).state == "completed"
    assert len(supplier.review_calls) == 1


def test_only_successful_requested_inferred_dates_refresh_review_age(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    index = HistoryDownloadIndex(path, sqlite_policy=SqlitePolicyFixture())
    task = HistoryDownloadTask("600001", DAYS)
    for sequence, cutoff, reviewed in ((1, DAYS[4], ()), (2, DAYS[-1], ()), (3, DAYS[-1], (DAYS[0],))):
        index.begin(HistoryDownloadBatch(sequence, "a" * 64, "b" * 64, "c" * 64, cutoff), (task,))
        index.finish((HistoryDownloadCompletion(task, (), None, inferred_suspended_dates=(DAYS[0],), reviewed_dates=reviewed),))
        with sqlite3.connect(path) as connection:
            checked = connection.execute("SELECT checked_cutoff FROM download_inferred_suspension").fetchone()[0]
        assert DecodeDate(checked) == (DAYS[-1] if reviewed else DAYS[4])
    with pytest.raises(ValueError, match="history_download_completion_invalid"):
        HistoryDownloadCompletion(task, (), "tencent_history_pending", reviewed_dates=(DAYS[0],))


def test_review_due_uses_five_exchange_sessions_instead_of_old_price_date(tmp_path):
    path = tmp_path / "catalog.sqlite3"
    HistoryControlIndex(path, sqlite_policy=SqlitePolicyFixture()).initialize()
    review = HistoryInferredReviewIndex(path, sqlite_policy=SqlitePolicyFixture())
    review.initialize()
    cell = replace(_download("600001", (DAYS[0],)).batch.cells[0], status="tencent_inferred_suspended", unadjusted=None, qfq=None)
    partition = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    partition.initialize()
    partition.save_revisions((HistoryRevision(1, "main", cell, None, None, None),))
    review.checked("600001", (cell,), 1, DAYS[4], _budget())
    prices = HistoryDirectPrices(tmp_path, 1, None, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=SqlitePolicyFixture())
    four_sessions = FakeSupplier(DAYS[:9]).load_context(DAYS[8], 9)
    five_sessions = FakeSupplier(DAYS).load_context(DAYS[-1], len(DAYS))
    assert review.plan(four_sessions, prices, 1, PriceReviewConfiguration(), _budget(), lambda: False) == ()
    tasks = review.plan(five_sessions, prices, 1, PriceReviewConfiguration(), _budget(), lambda: False)
    assert [(task.code, task.inferred_dates) for task in tasks] == [("600001", (DAYS[0],))]


@pytest.mark.parametrize("returned_failure", [False, True])
def test_review_anchor_failure_preserves_age_and_published_prices(tmp_path, monkeypatch, returned_failure):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    original = HistoryDirectPrices.prepare

    class ConflictingReview(_InferredSupplier):
        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            window = super().fetch_tencent_window(security, dates, request_budget=request_budget)
            if request_budget is None or returned_failure:
                return window
            return replace(window, cells=tuple(
                replace(cell, unadjusted=replace(cell.unadjusted, volume=cell.unadjusted.volume * 10))
                if cell.trade_date == DAYS[-1] else cell for cell in window.cells
            ))

    def rejected(prices, *args, **kwargs):
        return replace(original(prices, *args, **kwargs), failure="history_tail_raw_price_conflict")

    if returned_failure:
        monkeypatch.setattr(HistoryDirectPrices, "prepare", rejected)
    failed = _run(tmp_path, ConflictingReview(inferred=False), budget=_budget())
    assert failed.state == "already_current"
    assert _snapshot(tmp_path) == before
    assert failed.unresolved_price_cells == 0
    monkeypatch.setattr(HistoryDirectPrices, "prepare", original)
    supplier = _InferredSupplier(inferred=False)
    assert _run(tmp_path, supplier, budget=_budget()).state == "completed"
    assert len(supplier.review_calls) == 1


def test_review_finds_a_qualified_anchor_before_an_inferred_only_tail(tmp_path):
    class SuspendedTail(_InferredSupplier):
        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            window = super().fetch_tencent_window(security, dates, request_budget=request_budget)
            return replace(window, cells=tuple(
                replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                if security.code == "600001" and (cell.trade_date in DAYS[-5:] or self.inferred and cell.trade_date == DAYS[1])
                else cell for cell in window.cells
            ))

    assert _run(tmp_path, SuspendedTail()).state == "completed"
    _legacy_metadata(tmp_path)
    supplier = SuspendedTail(inferred=False)
    result = _run(tmp_path, supplier, budget=_budget())
    assert result.state == "completed"
    assert len(supplier.review_calls) == 1
    assert DAYS[4] in supplier.review_calls[0][1]
    partition = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    assert partition.read_price_keys((("600001", DAYS[1]),), 2)[0].cell.status == "complete"


def test_review_lock_contention_defers_without_normal_lock_retries(tmp_path, monkeypatch):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    waits = []
    timeouts = []
    original = history_sqlite_read_connection.OpenSqliteConnection

    def locked(path, options, *, statement_trace, sqlite_policy):
        timeouts.append(sqlite_policy.connection_timeout_seconds)
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(history_sqlite_read_connection, "OpenSqliteConnection", locked)
    monkeypatch.setattr(history_sqlite_read_connection.time, "sleep", waits.append)
    status = _run(tmp_path, _InferredSupplier(inferred=False), budget=_budget())
    assert status.state == "already_current"
    assert _snapshot(tmp_path) == before
    assert len(timeouts) == 1 and timeouts[0] <= 0.001
    assert waits == []
    prices = HistoryDirectPrices(tmp_path, 1, None, HistorySqliteActivity(lambda: None, lambda: False), sqlite_policy=SqlitePolicyFixture())
    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        prices.read("600001", DAYS)
    assert len(timeouts) == 4
    assert timeouts[-1] == SqlitePolicyFixture().connection_timeout_seconds
    assert waits == [0.1, 0.2]
    monkeypatch.setattr(history_sqlite_read_connection, "OpenSqliteConnection", original)
    assert _run(tmp_path, _InferredSupplier(inferred=False), budget=_budget()).state == "completed"


def test_normal_gap_download_finishes_before_additional_inferred_review(tmp_path):
    initial = _InferredSupplier()
    initial.fail_code = "600002"
    assert _run(tmp_path, initial).unresolved_price_cells == len(DAYS)
    _legacy_metadata(tmp_path)

    class OrderedSupplier(_InferredSupplier):
        def __init__(self):
            super().__init__(inferred=False)
            self.order = []

        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            self.order.append(("review" if request_budget is not None else "normal", security.code))
            return super().fetch_tencent_window(security, dates, request_budget=request_budget)

    supplier = OrderedSupplier()
    status = _run(tmp_path, supplier, budget=_budget())
    assert status.state == "completed", status.reason
    assert status.unresolved_price_cells == 0
    assert supplier.order == [("normal", "600002"), ("review", "600001")]


def test_review_uses_current_source_owner_when_initial_flow_inventory_is_stale(tmp_path, monkeypatch):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    monkeypatch.setattr(HistoryPriceRepair, "baostock_codes", lambda _repair: frozenset())
    monkeypatch.setattr(HistoryPriceRepair, "source", lambda _repair, _code, _sequence: "baostock")
    supplier = _InferredSupplier(inferred=False)
    status = _run(tmp_path, supplier, budget=_budget())
    assert status.state == "already_current"
    assert _snapshot(tmp_path) == before
    assert supplier.review_calls == []
    assert status.price_review.remaining_inferred_codes == 1


@pytest.mark.parametrize("after_durable_write", [False, True])
def test_review_budget_after_begin_preserves_pending_sequence_and_resumes(tmp_path, monkeypatch, capsys, after_durable_write):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    _assert_public_snapshot(tmp_path, before)
    original = HistoryDirectPrices.commit_for_review

    def exhausted(prices, results, budget):
        if after_durable_write:
            original(prices, results, budget)
        raise PriceReviewBudgetExhausted("deadline")

    monkeypatch.setattr(HistoryDirectPrices, "commit_for_review", exhausted)
    deferred = _run(tmp_path, _InferredSupplier(inferred=False), budget=_budget())
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    pending = index.pending(before.sequence)
    assert deferred.state == "blocked"
    assert deferred.reason == "history_review_deferred"
    assert deferred.snapshot_hash == before.content_hash
    assert deferred.needs_retry
    assert _snapshot(tmp_path) == before
    _assert_public_snapshot(tmp_path, before)
    assert pending.sequence == before.sequence + 1
    StderrHistorySyncProgress().publish_result(deferred)
    assert "停牌复核待续" in capsys.readouterr().err
    partition = HistoryPricePartition(tmp_path / "prices_2026.sqlite3", 2026, 9, sqlite_policy=SqlitePolicyFixture())
    retained = partition.read_price_keys((("600001", DAYS[0]),), pending.sequence)[0].cell
    assert retained.status == ("complete" if after_durable_write else "tencent_inferred_suspended")

    monkeypatch.setattr(HistoryDirectPrices, "commit_for_review", original)
    supplier = _InferredSupplier(inferred=False)
    resumed = _run(tmp_path, supplier, budget=_budget())
    assert resumed.state == "completed", resumed.reason
    assert _snapshot(tmp_path).sequence == pending.sequence
    assert index.pending(pending.sequence) is None
    assert partition.read_price_keys((("600001", DAYS[0]),), before.sequence)[0].cell.status == "tencent_inferred_suspended"
    assert partition.read_price_keys((("600001", DAYS[0]),), pending.sequence)[0].cell.status == "complete"
    assert len(supplier.review_calls) == (0 if after_durable_write else 1)


@pytest.mark.parametrize("phase", ["fetch", "before_write", "after_write"])
def test_auxiliary_cancellation_reseals_old_public_snapshot(tmp_path, monkeypatch, phase):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    _assert_public_snapshot(tmp_path, before)
    original = HistoryDirectPrices.commit_for_review

    class CancelledSupplier(_InferredSupplier):
        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            if request_budget is not None and phase == "fetch":
                raise KeyboardInterrupt
            return super().fetch_tencent_window(security, dates, request_budget=request_budget)

    def cancelled(prices, results, budget):
        if phase == "after_write":
            original(prices, results, budget)
        raise KeyboardInterrupt

    if phase != "fetch":
        monkeypatch.setattr(HistoryDirectPrices, "commit_for_review", cancelled)
    status = _run(tmp_path, CancelledSupplier(inferred=False), budget=_budget())
    assert status.state == "cancelled"
    assert status.snapshot_hash == before.content_hash
    _assert_public_snapshot(tmp_path, before)
    pending = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).pending(before.sequence)
    assert (pending is None) == (phase == "fetch")
    monkeypatch.setattr(HistoryDirectPrices, "commit_for_review", original)
    assert _run(tmp_path, _InferredSupplier(inferred=False), budget=_budget()).state == "completed"


def test_context_failure_does_not_upgrade_or_invalidate_legacy_review_metadata(tmp_path):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as connection:
        connection.execute("DROP TABLE download_inferred_suspension")
        connection.execute("DROP TABLE download_inferred_cursor")
    HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).publish_file()
    _assert_public_snapshot(tmp_path, before)

    class FailedContext(_InferredSupplier):
        def load_context(self, *_args, **_kwargs):
            raise RuntimeError("fixture_context_unavailable")

    status = _run(tmp_path, FailedContext(), budget=_budget())
    assert status.state == "failed"
    _assert_public_snapshot(tmp_path, before)
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='download_inferred_suspension'").fetchall() == []


def test_cancellation_after_review_metadata_upgrade_keeps_old_public_snapshot(tmp_path, monkeypatch):
    assert _run(tmp_path, _InferredSupplier()).state == "completed"
    before = _snapshot(tmp_path)
    with sqlite3.connect(tmp_path / "catalog.sqlite3") as connection:
        connection.execute("DROP TABLE download_inferred_suspension")
        connection.execute("DROP TABLE download_inferred_cursor")
    HistoryControlIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).publish_file()
    original = HistoryInferredReviewIndex.initialize

    def cancelled(index):
        original(index)
        raise KeyboardInterrupt

    monkeypatch.setattr(HistoryInferredReviewIndex, "initialize", cancelled)
    status = _run(tmp_path, _InferredSupplier(inferred=False), budget=_budget())
    assert status.state == "cancelled"
    _assert_public_snapshot(tmp_path, before)
    assert HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).pending(before.sequence) is None


def test_auxiliary_cancellation_after_normal_download_keeps_old_public_reader(tmp_path):
    initial = _InferredSupplier()
    initial.fail_code = "600002"
    assert _run(tmp_path, initial).unresolved_price_cells == len(DAYS)
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)

    class CancelledReview(_InferredSupplier):
        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            if request_budget is not None:
                raise KeyboardInterrupt
            return super().fetch_tencent_window(security, dates)

    status = _run(tmp_path, CancelledReview(inferred=False), budget=_budget())
    assert status.state == "cancelled"
    _assert_public_snapshot(tmp_path, before)
    pending = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture()).pending(before.sequence)
    assert pending.sequence == before.sequence + 1
    supplier = _InferredSupplier(inferred=False)
    assert _run(tmp_path, supplier, budget=_budget()).state == "completed"
    assert all(code != "600002" for code, _dates in supplier.calls)


def test_cross_year_partial_review_reseals_only_written_year_and_preserves_old_sequence(tmp_path, monkeypatch):
    days = tuple(date(2025, 12, 25) + timedelta(days=offset) for offset in range(14))
    targets = (days[0], date(2026, 1, 1))
    configuration = replace(_configuration(tmp_path), sessions=len(days))

    class AcrossYears(FakeSupplier):
        def __init__(self, *, inferred):
            super().__init__(days)
            self.inferred = inferred

        def fetch_tencent_window(self, security, dates, *, request_budget=None):
            if request_budget is not None:
                request_budget.reserve_request(15.0)
                request_budget.reserve_request(15.0)
            window = super().fetch_tencent_window(security, dates)
            return replace(window, cells=tuple(
                replace(cell, status="tencent_inferred_suspended", unadjusted=None, qfq=None)
                if self.inferred and security.code == "600001" and cell.trade_date in targets else cell
                for cell in window.cells
            ))

    def run(supplier, budget=None):
        return RunHistorySync(
            configuration,
            _BudgetSupplier(supplier, configuration.reference_root / "company_st.sqlite3"),
            clock=lambda: NOW,
            price_review_budget=budget,
            sqlite_policy=SqlitePolicyFixture(),
        )

    assert run(AcrossYears(inferred=True)).state == "completed"
    before = _snapshot(tmp_path)
    _legacy_metadata(tmp_path)
    original = HistoryDirectPrices.commit_for_review
    original_seal = history_sync_runner.PublishSqliteFile
    sealed = []

    def partial(prices, results, budget):
        first_year = tuple(replace(result, revisions=tuple(revision for revision in result.revisions if revision.trade_date.year == 2025)) for result in results)
        original(prices, first_year, budget)
        raise PriceReviewBudgetExhausted("deadline")

    def seal(path, *args, **kwargs):
        sealed.append(path.name)
        return original_seal(path, *args, **kwargs)

    monkeypatch.setattr(HistoryDirectPrices, "commit_for_review", partial)
    monkeypatch.setattr(history_sync_runner, "PublishSqliteFile", seal)
    deferred = run(AcrossYears(inferred=False), _budget())
    assert deferred.state == "blocked"
    assert deferred.reason == "history_review_deferred"
    assert sealed == ["prices_2025.sqlite3"]
    history = LoadPublishedHistorySnapshot(tmp_path, sequence=before.sequence, sqlite_policy=SqlitePolicyFixture())
    assert history.snapshot == before
    assert LoadPublishedHistorySnapshot(tmp_path, sqlite_policy=SqlitePolicyFixture()).snapshot == before
    VerifyPublishedHistorySnapshot(history, sqlite_policy=SqlitePolicyFixture())
    rows = HistorySnapshotReader(tmp_path, sqlite_policy=SqlitePolicyFixture()).read_code_window("600001", days, before)
    assert tuple(row.trade_date for row in rows if row.cell.status == "tencent_inferred_suspended") == targets
    index = HistoryDownloadIndex(tmp_path / "catalog.sqlite3", sqlite_policy=SqlitePolicyFixture())
    pending = index.pending(before.sequence)
    for year, target, expected in ((2025, targets[0], "complete"), (2026, targets[1], "tencent_inferred_suspended")):
        partition = HistoryPricePartition(tmp_path / f"prices_{year}.sqlite3", year, target.month, sqlite_policy=SqlitePolicyFixture())
        assert partition.read_price_keys((("600001", target),), pending.sequence)[0].cell.status == expected
    monkeypatch.setattr(HistoryDirectPrices, "commit_for_review", original)
    assert run(AcrossYears(inferred=False), _budget()).state == "completed"
    assert _snapshot(tmp_path).sequence == pending.sequence
