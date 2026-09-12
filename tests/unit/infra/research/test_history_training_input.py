from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import trader.infra.research.history_training_due as due_module
from scripts.runtime_diagnostics.history_archive_performance import inspect_history_archive_performance
from trader.application.research.history_sync import HistorySupplierContext, HistorySyncConfiguration
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
from trader.domain.research.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.infra.research.history_archive_reader import HistoryPartitionRevisionComparison
from trader.infra.research.history_archive_sync import run_history_sync
from trader.infra.research.history_control_repository import SQLiteHistoryControlRepository
from trader.infra.research.history_training_due import _revised_dates_since_bundle, evaluate_history_training_due
from trader.infra.research.history_training_input import SQLiteHistoryTrainingInputArchive
from trader.infra.scoring.profiles.v3.training_bundle_repository import ActiveTomorrowBundle

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _side(code: str, day: date, adjustment: str) -> BaoStockDailySide:
    return BaoStockDailySide(
        code,
        day,
        adjustment,  # type: ignore[arg-type]
        10.0,
        10.0,
        10.0,
        10.0,
        100.0,
        1000.0,
        10.0 if adjustment == "unadjusted" else None,
        0.0 if adjustment == "unadjusted" else None,
        0.01 if adjustment == "unadjusted" else None,
        "trading",
    )


class _Supplier:
    def __init__(self, dates: tuple[date, ...]) -> None:
        self._dates = dates

    def load_context(self, _as_of: date, sessions: int) -> HistorySupplierContext:
        return HistorySupplierContext(
            BaoStockCalendar(self._dates[-sessions:]),
            (BaoStockSecurity("600001", "样本", "main", date(2000, 1, 1), None, "test"),),
            BaoStockSourceVersions("test", "3.12", ()),
            (BaoStockIndustryInterval("600001", date(2000, 1, 1), None, "银行", "test"),),
        )

    def fetch_code(self, security, dates):
        cells = tuple(
            BaoStockDailyCell(
                security.code,
                day,
                "complete",
                _side(security.code, day, "unadjusted"),
                _side(security.code, day, "qfq"),
            )
            for day in dates
        )
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, cells),
            tuple(BaoStockDailyFact(security.code, day, False) for day in dates),
        )


def test_monthly_training_input_binds_active_snapshot_and_counts_typed_rows(tmp_path: Path) -> None:
    archive_root = tmp_path / "history" / "baostock"
    configuration = HistorySyncConfiguration(archive_root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    result = run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW)

    assert result.state == "completed"
    archive = SQLiteHistoryTrainingInputArchive.open(tmp_path / "history")
    assert (
        archive.snapshot.active_snapshot_hash
        == SQLiteHistoryControlRepository(archive_root / "control.sqlite3").load_state().active_snapshot_hash
    )
    assert archive.count_training_rows(frozenset(dates)) == len(dates)
    assert archive.snapshot.label_cutoff == dates[-2]


def test_training_input_reports_exact_inspected_rows_from_one_verified_snapshot(tmp_path: Path) -> None:
    archive_root = tmp_path / "history" / "baostock"
    configuration = HistorySyncConfiguration(archive_root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW)
    archive = SQLiteHistoryTrainingInputArchive.open(tmp_path / "history")
    progress: list[tuple[int, int, int, int, int, str]] = []

    archive.verify_partitions(lambda *values: progress.append(values))
    assert archive.count_training_rows(frozenset(dates)) == 3
    assert archive.training_row_upper_bound(frozenset(dates)) >= 3
    assert progress[-1][:3] == (1, 1, 1)


def test_training_input_streams_windows_in_date_code_order_without_per_code_queries(tmp_path: Path) -> None:
    archive_root = tmp_path / "history" / "baostock"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(62))
    configuration = HistorySyncConfiguration(archive_root, sessions=62, reread_sessions=2, minimum_free_bytes=0)
    run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW)
    archive = SQLiteHistoryTrainingInputArchive.open(tmp_path / "history")
    progress: list[int] = []

    windows = tuple(archive.iter_training_windows(frozenset(dates), progress.append))

    assert tuple(window.trade_date for window in windows) == dates[60:]
    assert all(len(window.rows) == 61 for window in windows)
    assert progress[-1] == len(dates)
    assert archive.training_row_upper_bound(frozenset(dates)) >= progress[-1]


def test_history_archive_performance_diagnostic_is_bounded_and_never_writes_active_archive(
    tmp_path: Path,
) -> None:
    archive_root = tmp_path / "history" / "baostock"
    dates = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(70))
    configuration = HistorySyncConfiguration(archive_root, sessions=70, reread_sessions=2, minimum_free_bytes=0)
    run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW)
    before = {
        path.relative_to(archive_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive_root.rglob("*.sqlite3")
    }

    report = inspect_history_archive_performance(archive_root, 1, 1, 10)

    after = {
        path.relative_to(archive_root): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in archive_root.rglob("*.sqlite3")
    }
    assert before == after
    assert tuple(item.workload for item in report.queries) == (
        "latest_month",
        "single_code_window",
        "single_day_board",
    )
    assert report.revision_write.requested_rows == 10
    assert report.revision_write.transaction_count == 1


def test_training_due_uses_the_active_snapshot_label_cutoff_and_marks_initial(tmp_path: Path) -> None:
    archive_root = tmp_path / "history" / "baostock"
    configuration = HistorySyncConfiguration(archive_root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW)

    evaluation = evaluate_history_training_due(archive_root, tmp_path / "train", NOW)

    assert evaluation is not None
    assert evaluation.state.reason == "initial_training_required"
    assert evaluation.state.current_label_cutoff == dates[-2]
    assert evaluation.state.training_due is True


def test_normal_new_label_day_keeps_cadence_instead_of_forcing_snapshot_rebind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_root = tmp_path / "history" / "baostock"
    configuration = HistorySyncConfiguration(archive_root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    first_dates = (date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9))
    run_history_sync(configuration, _Supplier(first_dates), clock=lambda: NOW)
    baseline = SQLiteHistoryControlRepository(archive_root / "control.sqlite3").load_state().active_snapshot
    assert baseline is not None
    contract_hash = "9" * 64
    bundle = ActiveTomorrowBundle(
        tmp_path / "train/tomorrow-v3/model.json",
        baseline.content_hash,
        baseline.source_identity_hash,
        baseline.label_cutoff,
        contract_hash,
        "a" * 64,
        "b" * 64,
        "c" * 64,
    )
    later_dates = (*first_dates, date(2026, 9, 10))
    run_history_sync(configuration, _Supplier(later_dates), clock=lambda: NOW)
    monkeypatch.setattr(due_module, "_active_bundle", lambda _root: (bundle, False))

    evaluation = evaluate_history_training_due(archive_root, tmp_path / "train", NOW, contract_hash)

    assert evaluation is not None
    assert evaluation.state.reason == "not_due"
    assert evaluation.state.matured_label_days_since_training == 1
    assert evaluation.revised_dates == ()


def test_revision_detection_compares_semantic_rows_only_in_changed_months(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dates = tuple(date(2026, 9, day) for day in range(1, 6))
    baseline = HistoryActiveSnapshot(
        1,
        dates[-1],
        dates[-2],
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (HistorySnapshotPartition("partitions/2026/09.sqlite3", "d" * 64, 1),),
    )
    active = HistoryActiveSnapshot(
        2,
        dates[-1],
        dates[-2],
        "a" * 64,
        "b" * 64,
        "c" * 64,
        (HistorySnapshotPartition("partitions/2026/09.sqlite3", "e" * 64, 1),),
    )

    class _Archive:
        def __init__(self, _root: Path) -> None:
            pass

        def revised_dates(
            self,
            _start: date,
            _end: date,
            comparison: HistoryPartitionRevisionComparison,
        ) -> tuple[date, ...]:
            assert comparison.physical_reference == active.partitions[0]
            assert comparison.before_sequence == baseline.sequence
            assert comparison.after_sequence == active.sequence
            return (dates[2],)

    monkeypatch.setattr("trader.infra.research.history_training_due.SQLiteHistoryArchiveReader", _Archive)

    assert _revised_dates_since_bundle(tmp_path, baseline, active, dates, dates[-2]) == (dates[2],)
