from collections import Counter
from dataclasses import replace
from datetime import date, timedelta
import os
import sqlite3

import pytest

from tests.unit.infra.research.test_baostock_gateway import SparseRangeSdk
from tests.unit.infra.research.test_history_archive_sync import (
    NOW,
    FakeSupplier,
    ProgressRecorder,
    _configuration,
    _download,
)
from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockDailyCell,
    BaoStockDailySpec,
    BaoStockSecurity,
)
from trader.download.domain.history_control import HistorySnapshotPartition
from trader.download.domain.history_price_qualification import qualify_history_pairs
from trader.download.domain.history_revision import HistoryRevision
from trader.download.infra.baostock_gateway import BaoStockRowGateway
from trader.download.infra.history_archive_reader import SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_repository import SQLiteHistoryControlRepository
from trader.download.infra.history_gap_recovery import HistoryGapRecoveryContext, recover_selected_history_gaps
from trader.download.infra.history_gap_resume import HistoryGapResumeContext, reopen_legacy_history_gaps
from trader.download.infra.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.download.infra.history_pending_prices import prepare_pending, write_qualified_month
from trader.download.infra.history_revision_codec import encode_history_revision
from trader.download.infra.history_tencent_stage import HistorySealedMonthCheckpoint, HistoryTencentStage


def _context(tmp_path, supplier, codes, cancel=lambda: False):
    stage = HistoryTencentStage(tmp_path / "stage.sqlite3", "sync-test")
    stage.initialize()
    securities = tuple(BaoStockSecurity(code, code, "main", date(2000, 1, 1), None, "test") for code in codes)
    stage.save_batch((), tuple((code, "tencent_query_failed") for code in codes))
    stage.save_gap_inventory((code, day.isoformat(), "baostock_price_pair") for code in codes for day in supplier.dates)
    return HistoryGapRecoveryContext(stage, supplier, securities, tuple(codes), 1, ProgressRecorder(), cancel)


def test_all_codes_are_attempted_after_more_than_180_seconds(tmp_path):
    codes = tuple(f"60{i:04d}" for i in range(45))

    class Slow(FakeSupplier):
        elapsed = 0

        def fetch_baostock_prices(self, security, dates):
            self.elapsed += 20
            return super().fetch_baostock_prices(security, dates)

    supplier = Slow((date(2026, 9, 8),))
    context = _context(tmp_path, supplier, codes)
    assert recover_selected_history_gaps(context) is None
    assert supplier.elapsed == 900
    assert [code for code, _ in supplier.calls] == list(codes)
    assert all(context.stage.recovery_checkpoint(code).succeeded for code in codes)
    assert context.progress.values[-1].recovery_summary.succeeded_codes == 45
    supplier.calls.clear()
    recover_selected_history_gaps(context)
    assert supplier.calls == []


@pytest.mark.parametrize("failures", (0, 1, 2, 3))
@pytest.mark.parametrize("failure_kind", ("request", "incomplete"))
def test_three_attempt_limit_and_terminal_progress(tmp_path, failures, failure_kind):
    attempts = Counter()

    class Flaky(FakeSupplier):
        def fetch_baostock_prices(self, security, dates):
            attempts[security.code] += 1
            result = _download(security.code, dates)
            if security.code == "600001" and attempts[security.code] <= failures:
                if failure_kind == "request":
                    raise RuntimeError("supplier_daily_raw_timeout")
                return replace(
                    result,
                    batch=replace(
                        result.batch,
                        cells=tuple(replace(cell, qfq=None, status="qfq_missing") for cell in result.batch.cells),
                    ),
                )
            return result

    context = _context(tmp_path, Flaky((date(2026, 9, 8),)), ("600001", "600002"))
    recover_selected_history_gaps(context)
    assert attempts == {"600001": min(failures + 1, 3), "600002": 1}
    assert context.stage.recovery_checkpoint("600001").succeeded == (failures < 3)
    assert bool(context.stage.recovery_cells("600001")) == (failures < 3)
    summary = context.progress.values[-1].recovery_summary
    assert (summary.succeeded_codes, summary.failed_codes, summary.unattempted_codes) == (
        2 - int(failures == 3),
        int(failures == 3),
        0,
    )
    retry = [item for item in context.progress.values if item.state == "retrying"]
    assert all(item.completed_units == 0 for item in retry)
    recover_selected_history_gaps(context)
    assert attempts == {"600001": min(failures + 1, 3), "600002": 1}


def test_cancel_discards_late_result_and_resume_keeps_attempt_count(tmp_path):
    cancelled = False

    class Late(FakeSupplier):
        def fetch_baostock_prices(self, security, dates):
            nonlocal cancelled
            cancelled = True
            return _download(security.code, dates)

    context = _context(tmp_path, Late((date(2026, 9, 8),)), ("600001", "600002"), lambda: cancelled)
    with pytest.raises(RuntimeError, match="cancelled"):
        recover_selected_history_gaps(context)
    assert context.stage.recovery_checkpoint("600001").attempts == 1
    assert not context.stage.recovery_cells("600001")
    assert context.stage.recovery_checkpoint("600002").attempts == 0
    resumed = FakeSupplier(context.supplier.dates)
    context = replace(context, supplier=resumed, cancel_requested=lambda: False)
    recover_selected_history_gaps(context)
    assert context.stage.recovery_checkpoint("600001").attempts == 2
    assert context.stage.recovery_checkpoint("600002").attempts == 1


def test_dispatch_crash_cannot_create_a_fourth_attempt(tmp_path):
    context = _context(tmp_path, FakeSupplier((date(2026, 9, 8),)), ("600001",))
    for _ in range(3):
        context.stage.begin_recovery_attempt("600001")
    recover_selected_history_gaps(context)
    assert context.supplier.calls == []
    assert context.progress.values[-1].recovery_summary.failed_codes == 1


@pytest.mark.parametrize("kind", ("st", "identity"))
def test_fatal_supplier_conflict_stops_and_remains_fatal_on_resume(tmp_path, kind):
    class Conflict(FakeSupplier):
        def fetch_baostock_prices(self, security, dates):
            result = _download(security.code if kind == "st" else "600099", dates)
            return replace(result, daily_facts=tuple(replace(fact, is_st=True) for fact in result.daily_facts))

    context = _context(tmp_path, Conflict((date(2026, 9, 8),)), ("600001", "600002"))
    for _ in range(2):
        with pytest.raises(RuntimeError, match="conflict"):
            recover_selected_history_gaps(context)
    assert context.stage.recovery_checkpoint("600001").attempts == 1
    assert context.stage.recovery_checkpoint("600002").attempts == 0


def test_persistence_failure_stops_without_supplier_retry(tmp_path, monkeypatch):
    context = _context(tmp_path, FakeSupplier((date(2026, 9, 8),)), ("600001", "600002"))

    def fail(*_args):
        raise sqlite3.OperationalError("disk_full")

    monkeypatch.setattr(context.stage, "finish_recovery_attempt", fail)
    with pytest.raises(sqlite3.OperationalError, match="disk_full"):
        recover_selected_history_gaps(context)
    assert len(context.supplier.calls) == 1
    assert not any(item.state == "completed" for item in context.progress.values)


@pytest.mark.parametrize(
    "reason", ("history_tail_qfq_basis_conflict", "history_tail_raw_price_conflict", "supplier_daily_window_conflict")
)
def test_deterministic_conflict_stops_once_and_resume_does_not_retry(tmp_path, reason):
    class Conflict(FakeSupplier):
        def fetch_baostock_prices(self, security, dates):
            self.calls.append((security.code, dates))
            raise RuntimeError(reason)

    context = _context(tmp_path, Conflict((date(2026, 9, 8),)), ("600001",))
    recover_selected_history_gaps(context)
    recover_selected_history_gaps(context)
    assert len(context.supplier.calls) == 1
    assert context.stage.recovery_checkpoint("600001").attempts == 1
    assert context.progress.values[-1].recovery_summary.failed_codes == 1


def test_sparse_supplier_range_recovers_and_publishes_without_redundant_retries(tmp_path):
    dates = tuple(date(2026, 9, 1) + timedelta(days=index) for index in range(8))
    sdk = SparseRangeSdk()
    gateway = BaoStockRowGateway(sdk, python_version="fixture", dependency_versions=())

    class SparseSupplier(FakeSupplier):
        def fetch_tencent_window(self, security, requested):
            window = super().fetch_tencent_window(security, requested)
            if security.code == "600001":
                return replace(
                    window,
                    cells=(
                        replace(window.cells[0], status="unknown_missing", unadjusted=None, qfq=None),
                        *window.cells[1:],
                    ),
                )
            return window

        def fetch_baostock_prices(self, security, requested):
            self.calls.append((security.code, requested))
            return gateway.fetch_code_download(
                BaoStockDailySpec(sessions=len(requested), source_cutoff=requested[-1]),
                security,
                BaoStockCalendar(requested),
            )

    result = run_history_sync(
        replace(_configuration(tmp_path), sessions=8, reread_sessions=5), SparseSupplier(dates), clock=lambda: NOW
    )
    assert result.state == "completed" and result.unresolved_price_cells == 0
    assert len(sdk.calls) == 2
    active = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert all(row.cell.obtained for row in SQLiteHistoryArchiveReader(tmp_path).read_day(dates[0], active))


@pytest.mark.parametrize("layout", ("pending", "qualified", "legacy_candidates"))
def test_legacy_unresolved_checkpoint_is_reopened_and_recovered(tmp_path, layout):
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    context = _context(tmp_path, FakeSupplier(dates), ("600001",))
    stage = context.stage
    pending = prepare_pending(tmp_path, dates, None, False)
    valid = _download("600001", dates).batch.cells[0]
    missing = BaoStockDailyCell("600001", dates[1], "unknown_missing", None, None)
    revisions = tuple(HistoryRevision(1, "main", cell, None, None, None) for cell in (valid, missing))
    month = (2026, 9)
    if layout == "pending":
        SQLiteHistoryMonthPartitionRepository(pending.paths[month], *month).save_revisions(revisions)
        stage.mark_supplemented((("600001", 1),))
    elif layout == "qualified":
        stage.save_qualified((("600001", revisions, 1),))
    else:
        # An older checkpoint used revision JSON in candidates, tagged per code.
        # Completed prices had already moved into pending months; only sparse rows remained.
        SQLiteHistoryMonthPartitionRepository(pending.paths[month], *month).save_revisions(revisions)
        with sqlite3.connect(tmp_path / "stage.sqlite3") as connection:
            connection.executemany(
                "INSERT INTO candidates VALUES (?,?,?,?)",
                (
                    (item.code, item.trade_date.isoformat(), encode_history_revision(item), int(item.cell.obtained))
                    for item in revisions[1:]
                ),
            )
            connection.execute("INSERT INTO phase_state VALUES ('qualified:600001')")
        stage.mark_supplemented((("600001", 1),))
        stage.initialize()
    # Detaching a shared unpublished file must leave the other link byte-identical.
    with sqlite3.connect(pending.paths[month]) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    protected = tmp_path / "protected.sqlite3"
    os.link(pending.paths[month], protected)
    before = protected.read_bytes()
    requests = (("600001", dates),)
    reopen_legacy_history_gaps(HistoryGapResumeContext(stage, pending, 1, lambda: False), requests)
    recover_selected_history_gaps(context)

    window, _, _ = stage.read_supplement_context("600001")
    cells = qualify_history_pairs("600001", dates, window, stage.recovery_cells("600001"))
    stage.save_qualified((("600001", tuple(HistoryRevision(1, "main", cell, None, None, None) for cell in cells), 0),))
    write_qualified_month(pending, stage, month)
    # Repeated migration after interruption is idempotent, preserving recovered observations.
    reopen_legacy_history_gaps(HistoryGapResumeContext(stage, pending, 1, lambda: False), requests)
    rows = SQLiteHistoryMonthPartitionRepository(pending.paths[month], *month).read_code(
        "600001",
        dates[0],
        dates[-1],
        snapshot_sequence=1,
    )
    assert all(row.cell.obtained for row in rows) and len(rows) == 2
    assert protected.read_bytes() == before
    assert stage.recovery_checkpoint("600001").attempts == 1


@pytest.mark.parametrize("damaged", (False, True))
def test_sync_from_revision_candidate_checkpoint_publishes_or_reports_specific_failure(tmp_path, damaged):
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    cancelled = False

    class CancelAfterInventory:
        def publish(self, progress):
            nonlocal cancelled
            if progress.stage == "history_gap_inventory" and progress.state == "completed":
                cancelled = True

    config = _configuration(tmp_path)
    result = run_history_sync(
        config,
        FakeSupplier(dates),
        clock=lambda: NOW,
        progress=CancelAfterInventory(),
        cancel_requested=lambda: cancelled,
    )
    assert result.state == "cancelled", result.reason
    path = next((tmp_path / ".tencent-stage").glob("*.sqlite3"))
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM candidates")
        for code in ("600001", "600002"):
            cells = _download(code, dates).batch.cells
            if code == "600001":
                cells = (*cells[:-1], BaoStockDailyCell(code, dates[-1], "unknown_missing", None, None))
            for cell in cells:
                payload = encode_history_revision(HistoryRevision(1, "main", cell, None, None, None))
                if damaged and code == "600002":
                    payload = "{}"
                connection.execute(
                    "INSERT INTO candidates VALUES (?,?,?,?)",
                    (code, cell.trade_date.isoformat(), payload, int(cell.obtained)),
                )
            connection.execute("INSERT INTO phase_state VALUES (?)", (f"qualified:{code}",))
            connection.execute("INSERT INTO supplemented VALUES (?,?)", (code, int(code == "600001")))
        connection.execute("INSERT INTO gaps VALUES (? ,?,'baostock_price_pair')", ("600001", dates[-1].isoformat()))
    supplier = FakeSupplier(dates)
    result = run_history_sync(config, supplier, clock=lambda: NOW)
    active = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    if damaged:
        assert result.state == "failed"
        assert result.reason == "history_legacy_candidate_invalid"
        assert active is None and supplier.events == []
    else:
        assert result.state == "completed" and result.unresolved_price_cells == 0
        assert supplier.events == ["baostock_prices"]
        rows = SQLiteHistoryArchiveReader(tmp_path).read_day(dates[-1], active)
        assert len(rows) == 2 and all(row.cell.obtained for row in rows)
        assert not path.exists()


def test_legacy_recovery_batches_month_reads_and_keeps_unchanged_seal(tmp_path, monkeypatch):
    dates = (date(2026, 8, 31), date(2026, 9, 1))
    codes = tuple(f"60{index:04d}" for index in range(65))
    context = _context(tmp_path, FakeSupplier(dates), codes)
    pending = prepare_pending(tmp_path, dates, None, False)
    for index, month in enumerate(((2026, 8), (2026, 9))):
        rows = tuple(
            HistoryRevision(
                1,
                "main",
                _download(code, dates).batch.cells[0]
                if index == 0
                else BaoStockDailyCell(code, dates[1], "unknown_missing", None, None),
                None,
                None,
                None,
            )
            for code in codes
        )
        SQLiteHistoryMonthPartitionRepository(pending.paths[month], *month).save_revisions(rows)
        reference = HistorySnapshotPartition(f"partitions/2026/{month[1]:02d}.sqlite3", "a" * 64, 65)
        context.stage.save_sealed_month(*month, HistorySealedMonthCheckpoint.capture(pending.paths[month], reference))
    previous_seal = context.stage.sealed_month(2026, 8)
    context.stage.mark_supplemented(tuple((code, 1) for code in codes))
    reads = []
    original = SQLiteHistoryMonthPartitionRepository.read_price_keys

    def read(self, keys, sequence):
        reads.append(len(keys))
        return original(self, keys, sequence)

    monkeypatch.setattr(SQLiteHistoryMonthPartitionRepository, "read_price_keys", read)
    progress = ProgressRecorder()
    reopen_legacy_history_gaps(
        HistoryGapResumeContext(context.stage, pending, 1, lambda: False, progress),
        tuple((code, dates) for code in codes),
    )
    assert reads == [64, 64, 1, 1]  # Four monthly connections, formerly 130.
    assert context.stage.sealed_month(2026, 8) == previous_seal
    assert context.stage.sealed_month(2026, 9) is None
    assert progress.values[-1].state == "completed"
    with sqlite3.connect(pending.paths[(2026, 8)]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_observations").fetchone() == (65,)
    with sqlite3.connect(pending.paths[(2026, 9)]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM daily_observations").fetchone() == (0,)


@pytest.mark.parametrize("layout", ("candidate", "qualified", "pending"))
def test_old_qfq_rejection_reopens_once_without_redownloading_valid_candidates(tmp_path, layout):
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    context = _context(tmp_path, FakeSupplier(dates), ("600001", "600002"))
    stage = context.stage
    pair = _download("600001", dates).batch.cells[0]
    missing = BaoStockDailyCell("600001", dates[1], "unknown_missing", None, None)
    from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell

    window = PublishedHistoryWindow("600001", tuple(project_history_cell(cell) for cell in (pair, missing)))
    stage.save_batch((window,), ())
    pending = prepare_pending(tmp_path, dates, None, False)
    if layout != "candidate":
        revisions = tuple(HistoryRevision(1, "main", cell, None, None, None) for cell in (pair, missing))
        stage.save_qualified((("600001", revisions, 1),))
        if layout == "pending":
            write_qualified_month(pending, stage, (2026, 9))
    for code, reason in (("600001", "history_tail_qfq_basis_conflict"), ("600002", "supplier_daily_raw_timeout")):
        for _ in range(3):
            stage.begin_recovery_attempt(code)
            stage.finish_recovery_attempt(code, reason)
    with sqlite3.connect(tmp_path / "stage.sqlite3") as connection:
        connection.execute("DELETE FROM phase_state WHERE name='existing_price_priority'")
    stage.initialize()
    assert stage.recovery_checkpoint("600001").attempts == 0
    assert stage.recovery_checkpoint("600002").attempts == 3
    reopen_legacy_history_gaps(HistoryGapResumeContext(stage, pending, 1, lambda: False), (("600001", dates),))
    stage.initialize()
    recover_selected_history_gaps(context)
    assert stage.recovery_checkpoint("600001").succeeded
    assert stage.recovery_checkpoint("600001").attempts == 1
    assert [code for code, _ in context.supplier.calls] == ["600001"]
    stage.initialize()
    assert stage.recovery_checkpoint("600001").succeeded
    recovered, _, _ = stage.read_supplement_context("600001")
    assert recovered.cells[0] == project_history_cell(pair)


def test_same_cutoff_only_retries_gaps_but_new_day_rereads_both_codes(tmp_path):
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class Incomplete(FakeSupplier):
        def fetch_tencent_window(self, security, requested):
            window = super().fetch_tencent_window(security, requested)
            if security.code == "600001":
                return replace(
                    window,
                    cells=(
                        replace(window.cells[0], status="unknown_missing", unadjusted=None, qfq=None),
                        *window.cells[1:],
                    ),
                )
            return window

        def fetch_baostock_prices(self, security, requested):
            raise RuntimeError("supplier_daily_raw_timeout")

    config = _configuration(tmp_path)
    first = run_history_sync(config, Incomplete(dates), clock=lambda: NOW)
    assert first.state == "completed" and first.unresolved_price_cells == 1
    supplier = FakeSupplier(dates)
    second = run_history_sync(config, supplier, clock=lambda: NOW)
    assert second.state == "completed" and second.unresolved_price_cells == 0
    assert [code for code, _ in supplier.calls] == ["600001"]
    daily = FakeSupplier((*dates, date(2026, 9, 11)))
    third = run_history_sync(config, daily, clock=lambda: NOW + timedelta(days=1))
    assert third.state == "completed" and third.unresolved_price_cells == 0
    assert {code for code, _ in daily.calls} == {"600001", "600002"}


def test_cancelled_legacy_batch_resumes_without_losing_valid_prices(tmp_path, monkeypatch):
    dates = (date(2026, 9, 8), date(2026, 9, 9))
    context = _context(tmp_path, FakeSupplier(dates), ("600001", "600002"))
    pending = prepare_pending(tmp_path, dates, None, False)
    rows = tuple(
        HistoryRevision(1, "main", cell, None, None, None)
        for code in ("600001", "600002")
        for cell in (
            _download(code, dates).batch.cells[0],
            BaoStockDailyCell(code, dates[1], "unknown_missing", None, None),
        )
    )
    month = SQLiteHistoryMonthPartitionRepository(pending.paths[(2026, 9)], 2026, 9)
    month.save_revisions(sorted(rows, key=lambda item: (item.trade_date, item.code)))
    context.stage.mark_supplemented((("600001", 1), ("600002", 1)))
    cancelled = False
    reopen = context.stage.reopen_legacy_recovery

    def stop_after_one(window):
        nonlocal cancelled
        reopen(window)
        cancelled = True

    monkeypatch.setattr(context.stage, "reopen_legacy_recovery", stop_after_one)
    requests = (("600001", dates), ("600002", dates))
    with pytest.raises(KeyboardInterrupt):
        reopen_legacy_history_gaps(HistoryGapResumeContext(context.stage, pending, 1, lambda: cancelled), requests)
    monkeypatch.setattr(context.stage, "reopen_legacy_recovery", reopen)
    reopen_legacy_history_gaps(HistoryGapResumeContext(context.stage, pending, 1, lambda: False), requests)
    assert len(month.read_day(dates[0], snapshot_sequence=1)) == 2
    assert month.read_day(dates[1], snapshot_sequence=1) == ()
    for code, _ in requests:
        window, missing, _ = context.stage.read_supplement_context(code)
        assert window.cells[0].status == "complete" and missing == (dates[1].isoformat(),)
