from __future__ import annotations

import sqlite3
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
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
)
from trader.download.domain.published_history import PublishedHistoryWindow, project_history_cell
from trader.download.infra import history_archive_sync as history_sync_module
from trader.download.infra.history_archive_sync import run_history_sync
from trader.download.infra.history_control_index import HistoryControlIndex
from trader.download.infra.history_month_partition import HistoryMonthPrices
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader

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
                    project_history_cell(replace(cell, status="unknown_missing", unadjusted=None, qfq=None))
                    for cell in _download(security.code, dates).batch.cells
                ),
            )
        shift = 1.0 if security.code == self.changed_qfq_code else 0.0
        return PublishedHistoryWindow(
            security.code,
            tuple(project_history_cell(cell) for cell in _download(security.code, dates, shift).batch.cells),
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

    completed = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
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
    repeated = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)

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
    first = run_history_sync(configuration, supplier, clock=lambda: first_now)
    assert first.state == "completed", first.reason
    old_snapshot = _snapshot(tmp_path)
    old_rows = HistorySnapshotReader(tmp_path).read_day(days[0], old_snapshot)
    supplier.calls.clear()
    new_now = first_now + timedelta(days=gap)
    supplier.dates = days[: 20 + gap]
    result = run_history_sync(configuration, supplier, clock=lambda: new_now)
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
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert result.unresolved_price_cells == 0
    assert supplier.events == ["tencent", "tencent"]
    rows = HistorySnapshotReader(tmp_path).read_day(days[-1], _snapshot(tmp_path))
    assert len(rows) == 2 and all(row.cell.status == "unknown_missing" for row in rows)


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
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert result.unresolved_price_cells == 3
    assert "baostock_prices" not in supplier.events
    supplier.fail_code = None
    supplier.calls.clear()
    repaired = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert repaired.state == "completed", repaired.reason
    assert repaired.unresolved_price_cells == 0
    assert [code for code, _ in supplier.calls] == ["600001"]


def test_publication_failure_resumes_without_redownloading(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days)
    original = history_sync_module.publish_history_snapshot

    def fail(*args):
        raise OSError("disk_full")

    monkeypatch.setattr(history_sync_module, "publish_history_snapshot", fail)
    failed = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert failed.state == "failed"
    assert HistoryControlIndex(tmp_path / "control.sqlite3").load_state().active_snapshot is None
    monkeypatch.setattr(history_sync_module, "publish_history_snapshot", original)
    supplier.calls.clear()
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert not supplier.calls


def test_new_process_reuses_committed_months_before_progress_update(tmp_path, monkeypatch):
    from trader.download.infra.history_download_index import HistoryDownloadIndex

    days = (date(2026, 8, 31), date(2026, 9, 1), date(2026, 9, 2))
    supplier = FakeSupplier(days)
    original = HistoryDownloadIndex.finish

    def fail(*args):
        raise OSError("checkpoint_failed")

    monkeypatch.setattr(HistoryDownloadIndex, "finish", fail)
    assert run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "failed"
    monkeypatch.setattr(HistoryDownloadIndex, "finish", original)
    supplier.calls.clear()
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert not supplier.calls


def test_database_write_failure_is_fatal_not_baostock_retry(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days)

    def fail(*args):
        raise OSError("disk_full")

    monkeypatch.setattr(HistoryMonthPrices, "save_revision_batches", fail)
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.state == "failed"
    assert "baostock_prices" not in supplier.events


def test_logical_proof_rejects_corruption_and_survives_unpublished_appends(tmp_path):
    from trader.download.domain.history_revision import HistoryRevision
    from trader.download.infra.history_month_partition import HistoryMonthPartitionError

    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert run_history_sync(_configuration(tmp_path), FakeSupplier(days), clock=lambda: NOW).state == "completed"
    snapshot = _snapshot(tmp_path)
    path = tmp_path / "partitions/2026/09.sqlite3"
    month = HistoryMonthPrices(path, 2026, 9)
    month.save_revisions(
        (HistoryRevision(2, "main", _download("600001", (date(2026, 9, 11),)).batch.cells[0], None, None, None),)
    )
    HistoryMonthPrices.verify(path, snapshot.partitions[0])
    assert len(tuple(month.iter_verified_training_rows(snapshot.partitions[0]))) == 6
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE daily_records SET content_hash=? WHERE code='600001' AND trade_date=?",
            ("0" * 64, days[0].isoformat()),
        )
    with pytest.raises(HistoryMonthPartitionError):
        HistoryMonthPrices.verify(path, snapshot.partitions[0])


def test_old_database_format_is_rejected_without_conversion(tmp_path):
    path = tmp_path / "control.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE metadata(singleton INTEGER PRIMARY KEY,schema_identity TEXT)")
        connection.execute("INSERT INTO metadata VALUES(1,'history_control')")
    result = run_history_sync(
        _configuration(tmp_path),
        FakeSupplier((date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))),
        clock=lambda: NOW,
    )
    assert result.state == "failed"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT schema_identity FROM metadata").fetchone() == ("history_control",)


def test_cross_month_interruption_only_requests_missing_dates_and_five_anchors(tmp_path, monkeypatch):
    days = tuple(date(2026, 8, 1) + timedelta(days=i) for i in range(35))
    supplier = FakeSupplier(days)
    config = HistorySyncConfiguration(tmp_path, sessions=35, reread_sessions=5, minimum_free_bytes=0)
    original = HistoryMonthPrices.save_revision_batches

    def interrupt_september(self, revisions, **kwargs):
        values = tuple(revisions)
        if values and values[0].trade_date.month == 9:
            raise KeyboardInterrupt
        return original(self, values, **kwargs)

    monkeypatch.setattr(HistoryMonthPrices, "save_revision_batches", interrupt_september)
    assert run_history_sync(config, supplier, clock=lambda: NOW).state == "cancelled"
    monkeypatch.setattr(HistoryMonthPrices, "save_revision_batches", original)
    supplier.calls.clear()
    resumed = run_history_sync(config, supplier, clock=lambda: NOW)
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
    original = history_sync_module.publish_history_snapshot

    def fail(*args):
        raise OSError("interrupted_publication")

    monkeypatch.setattr(history_sync_module, "publish_history_snapshot", fail)
    assert run_history_sync(config, supplier, clock=lambda: NOW.replace(day=3)).state == "failed"
    monkeypatch.setattr(history_sync_module, "publish_history_snapshot", original)
    supplier.calls.clear()
    result = run_history_sync(config, supplier, clock=lambda: NOW)
    assert result.state == "completed", result.reason
    assert result.data_cutoff == days[-1]
    assert len(supplier.calls) == 2
    assert all(dates == days[-3:] for _, dates in supplier.calls)


def test_missing_control_with_existing_month_prices_requires_rebuild(tmp_path):
    path = tmp_path / "partitions/2026/09.sqlite3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing prices")
    supplier = FakeSupplier((date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)))
    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    assert result.reason == "history_format_requires_rebuild"
    assert not supplier.calls
    assert path.read_bytes() == b"existing prices"


def test_changed_reference_keeps_old_snapshot_readable_without_price_requests(tmp_path):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days, industry="old_industry")
    assert run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "completed"
    old = _snapshot(tmp_path)
    reader = HistorySnapshotReader(tmp_path)
    previous = reader.read_day(days[-1], old)
    supplier.industry = "new_industry"
    supplier.calls.clear()
    assert run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "completed"
    current = _snapshot(tmp_path)
    assert not supplier.calls
    assert old.partitions == current.partitions
    assert HistorySnapshotReader(tmp_path).read_day(days[-1], old) == previous
    assert reader.read_day(days[-1], current) != previous


def test_pending_batch_rejects_supplier_contract_change(tmp_path, monkeypatch):
    days = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(days)

    def fail(*args):
        raise OSError("interrupted_publication")

    monkeypatch.setattr(history_sync_module, "publish_history_snapshot", fail)
    assert run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW).state == "failed"

    class Changed(FakeSupplier):
        def load_context(self, as_of, sessions):
            context = super().load_context(as_of, sessions)
            return replace(context, source_versions=BaoStockSourceVersions("different", "3.12", ()))

    changed = Changed(days)
    result = run_history_sync(_configuration(tmp_path), changed, clock=lambda: NOW)
    assert result.reason == "history_resume_context_changed"
    assert not changed.calls
