from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_price_qualification import QualifyPriceOverlap
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
)
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra import history_sync_runner as history_sync_module
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_download_index import HistoryDownloadIndex
from trader.download.infra.history_month_partition import HistoryMonthPrices, HistoryMonthWriteSession
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.download.infra.history_storage_profile import HistoryWriteTuning
from trader.download.infra.history_sync_runner import RunHistorySync
from trader.infra.sqlite_connection import OpenSqliteConnection

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _side(code: str, day: date, adjustment: str, close: float) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,  # type: ignore[arg-type]
        close,
        close,
        close,
        close,
        100.0,
        1000.0,
        close if adjustment == "unadjusted" else None,
        0.0 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        "trading",
    )


def _download(code: str, dates: tuple[date, ...], qfq_shift: float = 0.0) -> BaoStockCodeDownload:
    cells = tuple(
        BaoStockDailyCell(
            code,
            day,
            "complete",
            _side(code, day, "unadjusted", float(day.day)),
            _side(code, day, "qfq", float(day.day) + qfq_shift),
        )
        for day in dates
    )
    return BaoStockCodeDownload(
        BaoStockCodeBatch(code, cells),
        tuple(BaoStockDailyFact(code, day, False) for day in dates),
    )


@dataclass
class FakeSupplier:
    dates: tuple[date, ...]
    fail_code: str | None = None
    changed_qfq_code: str | None = None
    industry: str | None = None
    calls: list[tuple[str, tuple[date, ...]]] = field(default_factory=list)
    events: list[str] = field(default_factory=list)

    def load_context(
        self,
        _as_of: date,
        sessions: int,
        *,
        universe: tuple[BaoStockSecurity, ...] | None = None,
    ) -> HistorySupplierContext:
        selected = self.dates[-sessions:]
        selected_universe = universe or tuple(
            BaoStockSecurity(code, code, "main", date(2000, 1, 1), None, "test") for code in ("600001", "600002")
        )
        intervals = (
            tuple(
                BaoStockIndustryInterval(code, date(2000, 1, 1), None, self.industry, "test")
                for code in (item.code for item in selected_universe)
            )
            if self.industry is not None
            else ()
        )
        return HistorySupplierContext(
            BaoStockCalendar(selected),
            selected_universe,
            BaoStockSourceVersions("test", "3.12", ()),
            intervals,
            tuple(HistoryStEvidence(item.code, _as_of, "clear") for item in selected_universe),
        )

    def fetch_code(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
    ) -> BaoStockCodeDownload:
        self.calls.append((security.code, dates))
        if security.code == self.fail_code:
            raise RuntimeError("supplier_query_failed")
        shift = 1.0 if security.code == self.changed_qfq_code else 0.0
        return _download(security.code, dates, shift)

    def fetch_tencent_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        self.events.append("tencent")
        self.calls.append((security.code, dates))
        if security.code == self.fail_code:
            return PublishedHistoryWindow(
                security.code,
                tuple(
                    ProjectHistoryCell(replace(cell, status="unknown_missing", unadjusted=None, qfq=None))
                    for cell in _download(security.code, dates).batch.cells
                ),
            )
        shift = 1.0 if security.code == self.changed_qfq_code else 0.0
        return PublishedHistoryWindow(
            security.code,
            tuple(ProjectHistoryCell(cell) for cell in _download(security.code, dates, shift).batch.cells),
        )

    def fetch_baostock_raw(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        self.events.append("baostock_raw")
        value = _download(security.code, dates)
        cells = tuple(replace(cell, qfq=None, status="qfq_missing") for cell in value.batch.cells)
        return BaoStockCodeDownload(BaoStockCodeBatch(security.code, cells), value.daily_facts)

    def fetch_baostock_prices(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        self.events.append("baostock_prices")
        return self.fetch_code(security, dates)


def _configuration(root: Path) -> HistorySyncConfiguration:
    return HistorySyncConfiguration(root, sessions=3, reread_sessions=2, minimum_free_bytes=0)


@pytest.mark.parametrize("status", ("qfq_missing", "unadjusted_missing", "tencent_inferred_suspended"))
def test_retry_without_a_new_pair_preserves_existing_partial_fact(status):
    day = date(2026, 9, 10)
    complete = _download("600001", (day,)).batch.cells[0]
    previous = replace(
        complete,
        status=status,
        unadjusted=complete.unadjusted if status == "qfq_missing" else None,
        qfq=complete.qfq if status == "unadjusted_missing" else None,
    )
    unknown = BaoStockDailyCell("600001", day, "unknown_missing", None, None)
    assert QualifyPriceOverlap((unknown,), (previous,), full_window=False) == (previous,)


def test_transient_month_reader_lock_allows_download_to_finish(tmp_path, monkeypatch):
    from trader.download.infra import history_sqlite_read_connection

    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert RunHistorySync(_configuration(tmp_path), FakeSupplier(days), clock=lambda: NOW).state == "completed"
    previous = _snapshot(tmp_path)
    original_connect = OpenSqliteConnection
    failed = False

    class FirstReader:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if sql == "PRAGMA cache_size=-8192":
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return self.connection.execute(sql, *args)

        def close(self):
            self.connection.close()

        def __getattr__(self, name):
            return getattr(self.connection, name)

    def connect(*args, **kwargs):
        nonlocal failed
        connection = original_connect(*args, **kwargs)
        if not failed and "partitions" in args[0].parts:
            failed = True
            return FirstReader(connection)
        return connection

    monkeypatch.setattr(history_sqlite_read_connection, "OpenSqliteConnection", connect)
    advanced = (*days[1:], date(2026, 9, 11))
    result = RunHistorySync(_configuration(tmp_path), FakeSupplier(advanced), clock=lambda: NOW + timedelta(days=1))
    assert failed and result.state == "completed" and result.unresolved_price_cells == 0
    assert _snapshot(tmp_path) != previous
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], previous)


@dataclass
class ProgressRecorder:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


def test_initial_sync_publishes_verified_snapshot_and_same_cutoff_is_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(dates)

    completed = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert supplier.events == ["tencent", "tencent"]
    state = HistoryControlIndex(tmp_path / "control.sqlite3").load_state()
    snapshot = state.active_snapshot
    assert snapshot is not None
    assert len(HistorySnapshotReader(tmp_path).read_day(dates[-1], snapshot)) == 2
    monkeypatch.setattr(
        HistoryMonthPrices,
        "verify",
        classmethod(lambda _cls, _path, _reference, _progress=None: pytest.fail("routine sync reverified snapshot")),
    )
    repeated = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)

    assert completed.state == "completed"
    assert completed.data_cutoff == dates[-1]
    assert repeated.state == "already_current"
    assert len(supplier.calls) == 2
    assert tuple(item.relative_path for item in snapshot.partitions) == ("partitions/2026/09.sqlite3",)


def _snapshot(root):
    result = HistoryControlIndex(root / "control.sqlite3").load_state().active_snapshot
    assert result is not None
    return result


@pytest.mark.parametrize("gap", [1, 7, 14])
def test_incremental_updates_only_new_days_and_keeps_cumulative_history(tmp_path, gap):
    days = tuple(date(2026, 8, 1) + timedelta(days=index) for index in range(40))
    supplier = FakeSupplier(days[:20])
    configuration = HistorySyncConfiguration(tmp_path, sessions=20, reread_sessions=5, minimum_free_bytes=0)
    first_now = NOW.replace(month=8, day=20)
    first = RunHistorySync(configuration, supplier, clock=lambda: first_now)
    assert first.state == "completed", first.reason
    old_snapshot = _snapshot(tmp_path)
    old_rows = HistorySnapshotReader(tmp_path).read_day(days[0], old_snapshot)
    supplier.calls.clear()
    new_now = first_now + timedelta(days=gap)
    supplier.dates = days[: 20 + gap]
    result = RunHistorySync(configuration, supplier, clock=lambda: new_now)
    assert result.state == "completed", result.reason
    assert len(supplier.calls) == 2
    assert all(len(requested) == gap + 5 for _, requested in supplier.calls)
    active = _snapshot(tmp_path)
    reader = HistorySnapshotReader(tmp_path)
    assert reader.read_day(days[0], old_snapshot) == old_rows
    assert reader.read_day(days[0], active) == old_rows
    assert len(reader.read_day(new_now.date(), active)) == 2
    assert not tuple(tmp_path.rglob("*.pending.sqlite3"))
    assert not (tmp_path / ".tencent-stage").exists()


def test_partial_tencent_pairs_are_committed_without_baostock(tmp_path):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class Partial(FakeSupplier):
        def fetch_tencent_window(self, security, dates):
            window = super().fetch_tencent_window(security, dates)
            return replace(
                window,
                cells=tuple(
                    replace(cell, status="qfq_missing", qfq=None) if cell.trade_date == days[-1] else cell
                    for cell in window.cells
                ),
            )

    supplier = Partial(days)
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert result.unresolved_price_cells == 2
    assert supplier.events == ["tencent", "tencent"]
    rows = HistorySnapshotReader(tmp_path).read_day(days[-1], _snapshot(tmp_path))
    assert len(rows) == 2 and all(row.cell.status == "qfq_missing" and row.cell.qfq is None for row in rows)


def test_tencent_request_failure_publishes_explicit_gaps_and_next_run_repairs(tmp_path):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class FailingTencent(FakeSupplier):
        def fetch_tencent_window(self, security, dates):
            if security.code == self.fail_code:
                self.events.append("tencent")
                self.calls.append((security.code, dates))
                raise RuntimeError("tencent_qfq_request_failed")
            return super().fetch_tencent_window(security, dates)

    supplier = FailingTencent(days, fail_code="600001")
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert result.unresolved_price_cells == 3
    assert "baostock_prices" not in supplier.events
    supplier.fail_code = None
    supplier.calls.clear()
    repaired = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert repaired.state == "completed", repaired.reason
    assert repaired.unresolved_price_cells == 0
    assert [code for code, _ in supplier.calls] == ["600001"]


def test_publication_failure_resumes_without_redownloading(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days)
    original = history_sync_module.PublishHistorySnapshot

    def fail(*args):
        raise OSError("disk_full")

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", fail)
    failed = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert failed.state == "failed"
    assert HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot is None
    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", original)
    supplier.calls.clear()
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert not supplier.calls


def test_unpublished_gap_repair_resumes_same_sequence_without_changing_complete_rows(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    class PendingSupplier(FakeSupplier):
        def fetch_tencent_window(self, security, dates):
            if security.code == self.fail_code:
                raise RuntimeError("tencent_request_failed")
            return super().fetch_tencent_window(security, dates)

    supplier = PendingSupplier(days, fail_code="600001")
    original_publish = history_sync_module.PublishHistorySnapshot

    def fail_publish(*args):
        raise OSError("publication_failed")

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", fail_publish)
    first = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert first.state == "failed"
    assert HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot is None
    assert HistoryDownloadIndex(tmp_path / "control.sqlite3").gaps_by_code().get("600001", ()) == days
    month = HistoryMonthPrices(tmp_path / "partitions/2026/09.sqlite3", 2026, 9)
    assert month.read_code("600001", days[0], days[-1], snapshot_sequence=1)[0].cell.status == "unknown_missing"
    complete_before = month.read_code("600002", days[0], days[-1], snapshot_sequence=1)

    supplier.fail_code = None
    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", original_publish)
    resumed = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)

    assert resumed.state == "completed", resumed.reason
    assert resumed.active_snapshot_hash is not None
    assert _snapshot(tmp_path).sequence == 1
    assert [code for code, _ in supplier.calls].count("600001") == 1
    repaired = month.read_code("600001", days[0], days[-1], snapshot_sequence=1)
    assert len(repaired) == len(days)
    assert all(row.cell.status == "complete" for row in repaired)
    assert month.read_code("600002", days[0], days[-1], snapshot_sequence=1) == complete_before


@pytest.mark.parametrize("initial_status", ("unknown_missing", "qfq_missing"))
def test_pending_batch_replaces_incomplete_day_before_first_publication(tmp_path, monkeypatch, initial_status):
    from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
    from trader.download.infra.history_download_index import HistoryDownloadIndex

    days = tuple(date(2026, 9, 3) + timedelta(days=index) for index in range(8))

    def configuration(root):
        return HistorySyncConfiguration(root, sessions=8, reread_sessions=5, minimum_free_bytes=0)

    class PartialFirstRun(FakeSupplier):
        partial = True

        def fetch_tencent_window(self, security, dates):
            window = super().fetch_tencent_window(security, dates)
            if not self.partial or security.code != "600001":
                return window
            raise HistorySupplierPendingError(
                replace(
                    window,
                    cells=tuple(
                        replace(
                            cell,
                            status=initial_status,
                            unadjusted=None if initial_status == "unknown_missing" else cell.unadjusted,
                            qfq=None,
                        )
                        if cell.trade_date == days[0]
                        else cell
                        for cell in window.cells
                    ),
                )
            )

    root = tmp_path / "resumed"
    supplier = PartialFirstRun(days)
    publish = history_sync_module.PublishHistorySnapshot

    def interrupt_publication(*_args):
        raise OSError("fixture_publication_interrupted")

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", interrupt_publication)
    assert RunHistorySync(configuration(root), supplier, clock=lambda: NOW).state == "failed"
    assert HistoryControlIndex(root / "control.sqlite3").load_state().active_snapshot is None
    assert len(HistoryDownloadIndex(root / "control.sqlite3").gaps_by_code().get("600001", ())) == 1

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", publish)
    supplier.partial = False
    supplier.calls.clear()
    resumed = RunHistorySync(configuration(root), supplier, clock=lambda: NOW)
    assert resumed.state == "completed" and resumed.unresolved_price_cells == 0
    assert supplier.calls == [("600001", (days[0], *days[-5:]))]
    active = _snapshot(root)
    assert len(HistorySnapshotReader(root).read_day(days[0], active)) == 2
    clean = tmp_path / "clean"
    assert RunHistorySync(configuration(clean), FakeSupplier(days), clock=lambda: NOW).state == "completed"
    assert active.content_hash == _snapshot(clean).content_hash


def test_new_process_reuses_committed_months_before_progress_update(tmp_path, monkeypatch):
    from trader.download.infra.history_download_index import HistoryDownloadIndex

    days = (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))
    supplier = FakeSupplier(days)
    original = HistoryDownloadIndex.finish

    def fail(*args):
        raise OSError("checkpoint_failed")

    monkeypatch.setattr(HistoryDownloadIndex, "finish", fail)
    assert RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "failed"
    monkeypatch.setattr(HistoryDownloadIndex, "finish", original)
    supplier.calls.clear()
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert not supplier.calls


def test_database_write_failure_is_fatal_not_baostock_retry(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days)

    def fail(*args):
        raise OSError("disk_full")

    monkeypatch.setattr(HistoryMonthWriteSession, "save_revision_batches", fail)
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "failed"
    assert "baostock_prices" not in supplier.events


@pytest.mark.parametrize("boundary", ("read", "read_cleanup", "write", "checkpoint"))
def test_failure_reports_actual_boundary_without_payload_and_preserves_snapshot(
    tmp_path, monkeypatch, capsys, boundary
):
    from trader.download.entrypoints.history_sync_progress import StderrHistorySyncProgress
    from trader.download.infra.history_direct_prices import HistoryDirectPrices
    from trader.download.infra.history_download_index import HistoryDownloadIndex

    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    config = _configuration(tmp_path)
    assert RunHistorySync(config, FakeSupplier(days), clock=lambda: NOW).state == "completed"
    previous = _snapshot(tmp_path)

    def fail(*args):
        try:
            raise sqlite3.OperationalError("private payload token=secret /private/data/file")
        except sqlite3.Error as exc:
            raise RuntimeError("private wrapper text") from exc

    if boundary.startswith("read"):
        monkeypatch.setattr(HistoryDirectPrices, "read_for_download", fail)
        if boundary == "read_cleanup":

            def fail_close(*args):
                raise RuntimeError("cleanup_failure")

            monkeypatch.setattr(HistoryDirectPrices, "close_pipeline", fail_close)
    elif boundary == "write":
        monkeypatch.setattr(HistoryMonthWriteSession, "save_revision_batches", fail)
    else:
        monkeypatch.setattr(HistoryDownloadIndex, "finish", fail)
    reporter = StderrHistorySyncProgress(monotonic=lambda: 0)
    reporter.publish(HistorySyncProgress("tencent_history", "completed", 1, 2, "600361"))
    advanced = FakeSupplier((*days[1:], date(2026, 9, 11)))
    result = RunHistorySync(config, advanced, clock=lambda: NOW + timedelta(days=1), progress=reporter)
    reporter.publish_result(result)
    assert result.state == "failed" and result.reason == "history_sqlite_failed"
    assert _snapshot(tmp_path) == previous
    output = capsys.readouterr().err
    assert "OperationalError" in output and "test_history_sync.py:fail:" in output
    assert "secret" not in output and "private" not in output
    final = output.splitlines()[-1]
    assert "600361" not in final
    if boundary.startswith("read"):
        assert "股票 600001" in final
    else:
        label = "下载断点提交" if boundary == "checkpoint" else "正式月库提交"
        assert label in final and "股票" not in final


def test_logical_proof_rejects_corruption_and_survives_unpublished_appends(tmp_path):
    from trader.download.domain.history_revision import HistoryRevision
    from trader.download.infra.history_month_partition import HistoryMonthPartitionError

    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert RunHistorySync(_configuration(tmp_path), FakeSupplier(days), clock=lambda: NOW).state == "completed"
    snapshot = _snapshot(tmp_path)
    path = tmp_path / "partitions/2026/09.sqlite3"
    month = HistoryMonthPrices(path, 2026, 9)
    month.save_revisions(
        (HistoryRevision(2, "main", _download("600001", (date(2026, 9, 11),)).batch.cells[0], None, None, None),)
    )
    HistoryMonthPrices.verify(path, snapshot.partitions[0])
    assert (
        len(tuple(month.iter_training_rows(days[0], days[-1], snapshot_sequence=snapshot.partitions[0].sequence))) == 6
    )
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute(
            "UPDATE daily_records SET content_hash=? WHERE code='600001' AND trade_date=?",
            ("0" * 64, days[0].isoformat()),
        )
    with pytest.raises(HistoryMonthPartitionError):
        HistoryMonthPrices.verify(path, snapshot.partitions[0])


def test_old_database_format_is_rejected_without_conversion(tmp_path):
    path = tmp_path / "control.sqlite3"
    with closing(OpenSqliteConnection(path)) as connection, connection:
        connection.execute("CREATE TABLE metadata(singleton INTEGER PRIMARY KEY,schema_identity TEXT)")
        connection.execute("INSERT INTO metadata VALUES(1,'history_control')")
    result = RunHistorySync(
        _configuration(tmp_path),
        FakeSupplier((date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))),
        clock=lambda: NOW,
    )
    assert result.state == "failed"
    with closing(OpenSqliteConnection(path)) as connection, connection:
        assert connection.execute("SELECT schema_identity FROM metadata").fetchone() == ("history_control",)


def test_cross_month_interruption_only_requests_missing_dates_and_five_anchors(tmp_path, monkeypatch):
    days = tuple(date(2026, 8, 1) + timedelta(days=i) for i in range(35))
    supplier = FakeSupplier(days)
    config = HistorySyncConfiguration(tmp_path, sessions=35, reread_sessions=5, minimum_free_bytes=0)
    original = HistoryMonthWriteSession.save_revision_batches

    def interrupt_september(self, revisions, **kwargs):
        values = tuple(revisions)
        if values and values[0].trade_date.month == 9:
            raise KeyboardInterrupt
        return original(self, values, **kwargs)

    monkeypatch.setattr(HistoryMonthWriteSession, "save_revision_batches", interrupt_september)
    assert RunHistorySync(config, supplier, clock=lambda: NOW).state == "cancelled"
    monkeypatch.setattr(HistoryMonthWriteSession, "save_revision_batches", original)
    supplier.calls.clear()
    resumed = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert resumed.state == "completed", resumed.reason
    assert len(supplier.calls) == 2
    assert all(dates == days[-9:] for _, dates in supplier.calls)
    assert len(HistorySnapshotReader(tmp_path).read_day(days[0], _snapshot(tmp_path))) == 2


def test_resume_finishes_old_cutoff_then_updates_to_current_cutoff(tmp_path, monkeypatch):
    class Dated(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            dates = tuple(day for day in self.dates if day <= as_of)[-sessions:]
            return replace(context, calendar=BaoStockCalendar(dates))

    days = tuple(date(2026, 9, 1) + timedelta(days=i) for i in range(10))
    supplier = Dated(days)
    config = HistorySyncConfiguration(tmp_path, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    original = history_sync_module.PublishHistorySnapshot

    def fail(*args):
        raise OSError("interrupted_publication")

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", fail)
    assert RunHistorySync(config, supplier, clock=lambda: NOW.replace(day=3)).state == "failed"
    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", original)
    supplier.calls.clear()
    result = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert result.data_cutoff == days[-1]
    assert len(supplier.calls) == 2
    assert all(dates == days[-3:] for _, dates in supplier.calls)


def test_missing_control_with_existing_month_prices_requires_rebuild(tmp_path):
    path = tmp_path / "partitions/2026/09.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing prices")
    supplier = FakeSupplier((date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)))
    result = RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.reason == "history_format_requires_rebuild"
    assert not supplier.calls
    assert path.read_bytes() == b"existing prices"


def test_changed_reference_keeps_old_snapshot_readable_without_price_requests(tmp_path):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days, industry="old_industry")
    assert RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "completed"
    old = _snapshot(tmp_path)
    reader = HistorySnapshotReader(tmp_path)
    previous = reader.read_day(days[-1], old)
    supplier.industry = "new_industry"
    supplier.calls.clear()
    assert RunHistorySync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "completed"
    current = _snapshot(tmp_path)
    assert not supplier.calls
    assert old.partitions == current.partitions
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], old) == previous
    assert reader.read_day(days[-1], current) != previous


@pytest.mark.parametrize(
    ("versions", "renamed"),
    (
        (BaoStockSourceVersions("different", "3.12", ()), False),
        (BaoStockSourceVersions("test", "3.14", ()), False),
        (BaoStockSourceVersions("test", "3.12", (("pandas", "new"),)), False),
        (BaoStockSourceVersions("test", "3.12", ()), True),
        (BaoStockSourceVersions("test", "3.14", (("pandas", "new"),)), True),
    ),
)
def test_pending_batch_rebinds_versions_and_names_without_redownloading(tmp_path, monkeypatch, versions, renamed):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days)
    config = _configuration(tmp_path)
    assert RunHistorySync(config, supplier, clock=lambda: NOW).state == "completed"
    old = _snapshot(tmp_path)
    reader = HistorySnapshotReader(tmp_path)
    old_rows = reader.read_day(days[-1], old)
    supplier.dates = (*days[1:], date(2026, 9, 11))
    original = history_sync_module.PublishHistorySnapshot

    def fail(*args):
        raise OSError("interrupted_publication")

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", fail)
    now = NOW + timedelta(days=1)
    assert RunHistorySync(config, supplier, clock=lambda: now).state == "failed"
    index = HistoryDownloadIndex(tmp_path / "control.sqlite3")
    pending = index.pending(old.sequence)
    tasks = index.tasks()

    class Changed(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            universe = tuple(replace(item, name="新简称") for item in context.universe) if renamed else context.universe
            return replace(context, source_versions=versions, universe=universe)

    changed = Changed(supplier.dates)
    # A second interruption after rebinding must leave the active snapshot and tasks intact.
    assert RunHistorySync(config, changed, clock=lambda: now).state == "failed"
    rebound = index.pending(old.sequence)
    assert pending is not None and rebound is not None
    assert rebound.sequence == pending.sequence and rebound.cutoff == pending.cutoff
    assert rebound != pending
    assert index.tasks() == tasks
    assert _snapshot(tmp_path) == old
    assert reader.read_day(days[-1], old) == old_rows

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", original)
    result = RunHistorySync(config, changed, clock=lambda: now)
    assert result.state == "completed", result.reason
    assert result.data_cutoff == supplier.dates[-1]
    assert not changed.calls
    current = _snapshot(tmp_path)
    assert current.source_identity_hash == rebound.source_hash
    state = HistoryControlIndex(tmp_path / "control.sqlite3").load_state()
    latest_universe = next(item for item in state.universes if item.content_hash == current.universe_hash)
    if renamed:
        assert all(item.name == "新简称" for item in latest_universe.securities)
    assert len(reader.read_day(supplier.dates[-1], current)) == 2
    assert reader.read_day(days[-1], old) == old_rows
    with pytest.raises(RuntimeError, match="history_download_identity_conflict"):
        index.rebind_context(rebound, pending)


def test_changed_environment_resumes_only_unconfirmed_stock(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    config = replace(_configuration(tmp_path), history_workers=1)
    original_finish = HistoryDownloadIndex.finish

    def interrupt_after_confirmation(self, completions):
        original_finish(self, completions)
        raise KeyboardInterrupt

    monkeypatch.setattr(HistoryDownloadIndex, "finish", interrupt_after_confirmation)
    tuning = HistoryWriteTuning(1, 3, max_codes=1, max_cells=3)
    result = RunHistorySync(config, FakeSupplier(days), clock=lambda: NOW, write_tuning=tuning)
    assert result.state == "cancelled"
    index = HistoryDownloadIndex(tmp_path / "control.sqlite3")
    assert [task.tencent_done for task in index.tasks()] == [True, False]
    previous = index.pending(0)
    tasks = index.tasks()
    monkeypatch.setattr(HistoryDownloadIndex, "finish", original_finish)

    class Changed(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            return replace(
                context,
                source_versions=BaoStockSourceVersions("test", "3.14", ()),
                universe=tuple(replace(item, name="新简称") for item in context.universe),
            )

    supplier = Changed(days)
    original_rebind = HistoryDownloadIndex.rebind_context

    def interrupted_rebind(self, before, after):
        raise OSError("interrupted_before_rebind")

    monkeypatch.setattr(HistoryDownloadIndex, "rebind_context", interrupted_rebind)
    assert RunHistorySync(config, supplier, clock=lambda: NOW).state == "failed"
    assert index.pending(0) == previous and index.tasks() == tasks
    assert not supplier.calls
    monkeypatch.setattr(HistoryDownloadIndex, "rebind_context", original_rebind)
    result = RunHistorySync(config, supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert supplier.calls == [("600002", days)]
    assert len(HistorySnapshotReader(tmp_path).read_day(days[-1], _snapshot(tmp_path))) == 2


@pytest.mark.parametrize(
    ("change", "reason"),
    (
        ("calendar", "history_resume_calendar_changed"),
        ("board", "history_resume_security_changed"),
        ("listed_on", "history_resume_security_changed"),
        ("delisted_on", "history_resume_security_changed"),
        ("codes", "history_resume_codes_changed"),
        ("name", "history_resume_st_conflict"),
    ),
)
def test_pending_batch_still_rejects_changed_dates_or_securities(tmp_path, monkeypatch, change, reason):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))

    def fail(*args):
        raise OSError("interrupted_publication")

    monkeypatch.setattr(history_sync_module, "PublishHistorySnapshot", fail)
    assert RunHistorySync(_configuration(tmp_path), FakeSupplier(days), clock=lambda: NOW).state == "failed"
    index = HistoryDownloadIndex(tmp_path / "control.sqlite3")
    pending, tasks = index.pending(0), index.tasks()

    class Changed(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            context = replace(context, source_versions=BaoStockSourceVersions("new", "3.14", ()))
            first, second = context.universe
            if change == "calendar":
                return replace(context, calendar=BaoStockCalendar((date(2026, 9, 7), *days[1:])))
            if change == "codes":
                return replace(context, universe=(first,))
            replacements = {
                "name": "*ST风险",
                "board": "star",
                "listed_on": date(2001, 1, 1),
                "delisted_on": date(2026, 9, 9),
            }
            return replace(context, universe=(replace(first, **{change: replacements[change]}), second))

    changed = Changed(days)
    result = RunHistorySync(_configuration(tmp_path), changed, clock=lambda: NOW)
    assert result.reason == reason
    assert not changed.calls
    assert index.pending(0) == pending and index.tasks() == tasks
