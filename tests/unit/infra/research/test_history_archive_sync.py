from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from trader.application.research.history_sync import (
    HistorySupplierContext,
    HistorySyncConfiguration,
    HistorySyncProgress,
)
from trader.domain.research.baostock_daily import (
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
from trader.infra.research.history_archive_repack import HistoryArchiveRepackFenceError
from trader.infra.research.history_archive_sync import run_history_sync
from trader.infra.research.history_control_repository import SQLiteHistoryControlRepository
from trader.infra.research.history_month_archive import SQLiteHistoryMonthlyArchive

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

    def load_context(self, _as_of: date, sessions: int) -> HistorySupplierContext:
        selected = self.dates[-sessions:]
        universe = tuple(
            BaoStockSecurity(code, code, "main", date(2000, 1, 1), None, "test") for code in ("600001", "600002")
        )
        intervals = (
            tuple(
                BaoStockIndustryInterval(code, date(2000, 1, 1), None, self.industry, "test")
                for code in ("600001", "600002")
            )
            if self.industry is not None
            else ()
        )
        return HistorySupplierContext(
            BaoStockCalendar(selected),
            universe,
            BaoStockSourceVersions("test", "3.12", ()),
            intervals,
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


def _configuration(root: Path) -> HistorySyncConfiguration:
    return HistorySyncConfiguration(root, sessions=3, reread_sessions=2, minimum_free_bytes=0)


@dataclass
class ProgressRecorder:
    values: list[HistorySyncProgress] = field(default_factory=list)

    def publish(self, progress: HistorySyncProgress) -> None:
        self.values.append(progress)


def test_history_sync_configuration_owns_bounded_supplier_resources(tmp_path: Path) -> None:
    assert HistorySyncConfiguration().minimum_free_bytes == 10 * 1024**3
    with pytest.raises(ValueError, match="configuration"):
        HistorySyncConfiguration(tmp_path, query_interval_seconds=1.99)
    with pytest.raises(ValueError, match="configuration"):
        HistorySyncConfiguration(tmp_path, supplier_retries=3)
    with pytest.raises(ValueError, match="configuration"):
        HistorySyncConfiguration(tmp_path, cancellation_grace_seconds=10.01)
    with pytest.raises(ValueError, match="configuration"):
        HistorySyncConfiguration(tmp_path, supplier_timeout_seconds=4.0, progress_heartbeat_seconds=5.0)


@pytest.mark.parametrize(
    ("observed_at", "expected_as_of"),
    (
        (datetime(2026, 9, 10, 0, 10, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 9, 9)),
        (datetime(2026, 9, 10, 15, 10, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 9, 9)),
        (datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai")), date(2026, 9, 10)),
    ),
)
def test_history_sync_requests_only_completed_daily_data(
    tmp_path: Path,
    observed_at: datetime,
    expected_as_of: date,
) -> None:
    requested_dates: list[date] = []

    class _RecordingSupplier(FakeSupplier):
        def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
            requested_dates.append(as_of)
            return super().load_context(as_of, sessions)

    supplier = _RecordingSupplier((date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10)))

    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: observed_at)

    assert result.state == "completed"
    assert requested_dates == [expected_as_of]


def test_initial_sync_publishes_verified_snapshot_and_same_cutoff_is_noop(tmp_path: Path) -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(dates)

    completed = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)
    repeated = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)

    assert completed.state == "completed"
    assert completed.data_cutoff == dates[-1]
    assert repeated.state == "already_current"
    assert len(supplier.calls) == 2
    state = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state()
    snapshot = state.active_snapshot
    assert snapshot is not None
    assert len(SQLiteHistoryMonthlyArchive(tmp_path).read_day(dates[-1], snapshot)) == 2
    assert tuple(item.relative_path for item in snapshot.partitions) == ("partitions/2026/09.sqlite3",)


def test_initial_sync_fails_disk_preflight_before_slow_supplier_context(tmp_path: Path) -> None:
    supplier = FakeSupplier((date(2026, 9, 10),))
    configuration = HistorySyncConfiguration(
        tmp_path,
        sessions=1,
        reread_sessions=1,
        minimum_free_bytes=10**30,
    )

    result = run_history_sync(configuration, supplier, clock=lambda: NOW)

    assert result.state == "blocked"
    assert result.reason == "disk_space_insufficient"
    assert supplier.calls == []
    assert not tuple(tmp_path.glob("partitions/**/*.sqlite3"))


def test_history_sync_does_not_call_the_supplier_while_repack_activation_is_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supplier = FakeSupplier((date(2026, 9, 10),))
    monkeypatch.setattr(
        "trader.infra.research.history_archive_sync.require_history_repack_inactive",
        lambda _root: (_ for _ in ()).throw(HistoryArchiveRepackFenceError("fenced")),
    )

    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)

    assert result.state == "blocked"
    assert result.reason == "history_repack_activation_pending"
    assert supplier.calls == []


def test_history_sync_reports_context_code_sealing_and_publication_progress(tmp_path: Path) -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    recorder = ProgressRecorder()

    result = run_history_sync(
        _configuration(tmp_path),
        FakeSupplier(dates),
        clock=lambda: NOW,
        progress=recorder,
    )

    assert result.state == "completed"
    assert [(item.stage, item.state) for item in recorder.values] == [
        ("initializing", "started"),
        ("initializing", "completed"),
        ("loading_context", "started"),
        ("loading_context", "completed"),
        ("preparing_partitions", "started"),
        ("preparing_partitions", "completed"),
        ("downloading_codes", "started"),
        ("downloading_codes", "completed"),
        ("downloading_codes", "started"),
        ("downloading_codes", "completed"),
        ("sealing_partitions", "started"),
        ("sealing_partitions", "completed"),
        ("publishing_snapshot", "started"),
        ("publishing_snapshot", "completed"),
    ]
    assert recorder.values[6].current_item == "600001"
    assert recorder.values[6].completed_units == 0
    assert recorder.values[7].completed_units == 1
    assert recorder.values[9].completed_units == 2


def test_history_sync_cancels_cleanly_during_supplier_context_loading(tmp_path: Path) -> None:
    recorder = ProgressRecorder()

    class InterruptedSupplier(FakeSupplier):
        def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
            raise KeyboardInterrupt

    result = run_history_sync(
        _configuration(tmp_path),
        InterruptedSupplier((date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))),
        clock=lambda: NOW,
        progress=recorder,
    )

    assert result.state == "cancelled"
    assert result.reason == "cancelled"
    assert recorder.values[-1].stage == "loading_context"
    assert recorder.values[-1].state == "cancelled"
    assert SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().checkpoints == ()


def test_daily_sync_rereads_recent_dates_and_full_window_only_for_qfq_revision(tmp_path: Path) -> None:
    original = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    assert run_history_sync(_configuration(tmp_path), FakeSupplier(original), clock=lambda: NOW).state == "completed"
    old_snapshot = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot
    assert old_snapshot is not None
    old_row = SQLiteHistoryMonthlyArchive(tmp_path).read_day(original[-1], old_snapshot)[0]
    updated = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(updated, changed_qfq_code="600001")

    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)

    assert result.state == "completed"
    assert ("600001", updated) in supplier.calls
    assert ("600002", updated[-2:]) in supplier.calls
    assert [call for call in supplier.calls if call[0] == "600001"] == [
        ("600001", updated[-2:]),
        ("600001", updated),
    ]
    state = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state()
    assert state.active_snapshot is not None
    assert state.active_snapshot.sequence == 2
    new_row = SQLiteHistoryMonthlyArchive(tmp_path).read_day(updated[1], state.active_snapshot)[0]
    assert old_row.cell.qfq is not None and old_row.cell.qfq.close_price == 9.0
    assert new_row.cell.qfq is not None and new_row.cell.qfq.close_price == 10.0
    assert SQLiteHistoryMonthlyArchive(tmp_path).read_day(original[0], state.active_snapshot) == ()


def test_snapshot_publication_failure_restores_stable_month_and_old_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    assert run_history_sync(_configuration(tmp_path), FakeSupplier(original), clock=lambda: NOW).state == "completed"
    control = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    old_snapshot = control.load_state().active_snapshot
    assert old_snapshot is not None

    def fail_publish(_self, _snapshot) -> None:
        raise OSError("injected snapshot publication failure")

    monkeypatch.setattr(SQLiteHistoryControlRepository, "publish_snapshot", fail_publish)
    updated = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    failed = run_history_sync(_configuration(tmp_path), FakeSupplier(updated), clock=lambda: NOW)

    assert failed.state == "failed"
    assert control.load_state().active_snapshot == old_snapshot
    assert len(SQLiteHistoryMonthlyArchive(tmp_path).read_day(original[0], old_snapshot)) == 2
    assert not tuple((tmp_path / "partitions").glob("*/.*.rollback.sqlite3"))


def test_post_commit_failure_keeps_new_active_and_stable_month(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    assert run_history_sync(_configuration(tmp_path), FakeSupplier(original), clock=lambda: NOW).state == "completed"
    publish = SQLiteHistoryControlRepository.publish_snapshot

    def fail_after_publish(self, snapshot) -> None:
        publish(self, snapshot)
        raise OSError("injected post-commit failure")

    monkeypatch.setattr(SQLiteHistoryControlRepository, "publish_snapshot", fail_after_publish)
    updated = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    completed = run_history_sync(_configuration(tmp_path), FakeSupplier(updated), clock=lambda: NOW)
    active = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot

    assert completed.state == "completed"
    assert active is not None and active.sequence == 2
    assert len(SQLiteHistoryMonthlyArchive(tmp_path).read_day(updated[-1], active)) == 2
    assert not tuple((tmp_path / "partitions").glob("*/.*.rollback.sqlite3"))


def test_interrupted_stable_month_replacement_recovers_from_active_hash(tmp_path: Path) -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert run_history_sync(_configuration(tmp_path), FakeSupplier(dates), clock=lambda: NOW).state == "completed"
    stable = tmp_path / "partitions/2026/09.sqlite3"
    rollback = stable.with_name(".09.rollback.sqlite3")
    shutil.copyfile(stable, rollback)
    with stable.open("ab") as handle:
        handle.write(b"interrupted replacement")

    recovered = run_history_sync(_configuration(tmp_path), FakeSupplier(dates), clock=lambda: NOW)
    active = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state().active_snapshot

    assert recovered.state == "already_current"
    assert active is not None
    assert len(SQLiteHistoryMonthlyArchive(tmp_path).read_day(dates[-1], active)) == 2
    assert not rollback.exists()


def test_supplier_failure_and_cancellation_keep_active_pointer_and_resume_completed_codes(tmp_path: Path) -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    baseline = run_history_sync(_configuration(tmp_path), FakeSupplier(dates), clock=lambda: NOW)
    control = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    original_hash = control.load_state().active_snapshot_hash
    newer = (date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11))

    failed = run_history_sync(_configuration(tmp_path), FakeSupplier(newer, fail_code="600002"), clock=lambda: NOW)
    assert baseline.state == "completed"
    assert failed.state == "failed"
    assert control.load_state().active_snapshot_hash == original_hash

    checks = iter((False, True))
    cancelled_supplier = FakeSupplier(newer)
    cancelled = run_history_sync(
        _configuration(tmp_path),
        cancelled_supplier,
        clock=lambda: NOW,
        cancel_requested=lambda: next(checks, True),
    )
    assert cancelled.state == "cancelled"
    assert control.load_state().active_snapshot_hash == original_hash

    resumed_supplier = FakeSupplier(newer)
    resumed = run_history_sync(_configuration(tmp_path), resumed_supplier, clock=lambda: NOW)

    assert resumed.state == "completed"
    assert resumed_supplier.calls == []


def test_historical_industry_revision_refreshes_the_complete_active_window(tmp_path: Path) -> None:
    original = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    assert (
        run_history_sync(_configuration(tmp_path), FakeSupplier(original, industry="bank"), clock=lambda: NOW).state
        == "completed"
    )
    updated = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    supplier = FakeSupplier(updated, industry="finance")

    result = run_history_sync(_configuration(tmp_path), supplier, clock=lambda: NOW)

    assert result.state == "completed"
    assert supplier.calls == [("600001", updated), ("600002", updated)]


def test_daily_sync_reuses_untouched_immutable_months(tmp_path: Path) -> None:
    configuration = HistorySyncConfiguration(tmp_path, sessions=5, reread_sessions=2, minimum_free_bytes=0)
    original = (
        date(2026, 6, 30),
        date(2026, 7, 1),
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 9, 2),
    )
    assert run_history_sync(configuration, FakeSupplier(original), clock=lambda: NOW).state == "completed"
    control = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3")
    first = control.load_state().active_snapshot
    assert first is not None
    august = next(item for item in first.partitions if Path(item.relative_path).stem == "08")
    updated = (original[1], original[2], original[3], original[4], date(2026, 9, 3))

    assert run_history_sync(configuration, FakeSupplier(updated), clock=lambda: NOW).state == "completed"

    second = control.load_state().active_snapshot
    assert second is not None
    assert next(item for item in second.partitions if Path(item.relative_path).stem == "08") == august


def test_incomplete_late_payload_never_advances_cutoff(tmp_path: Path) -> None:
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    assert run_history_sync(_configuration(tmp_path), FakeSupplier(dates), clock=lambda: NOW).state == "completed"

    class IncompleteSupplier(FakeSupplier):
        def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
            value = super().fetch_code(security, dates)
            return _download(security.code, dates[:-1]) if security.code == "600002" else value

    result = run_history_sync(
        _configuration(tmp_path),
        IncompleteSupplier((date(2026, 9, 9), date(2026, 9, 10), date(2026, 9, 11))),
        clock=lambda: NOW,
    )
    state = SQLiteHistoryControlRepository(tmp_path / "control.sqlite3").load_state()

    assert result.state == "failed"
    assert result.reason == "supplier_data_incomplete"
    assert state.active_snapshot is not None and state.active_snapshot.data_cutoff == dates[-1]
