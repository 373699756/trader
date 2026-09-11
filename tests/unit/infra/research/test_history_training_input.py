from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
from trader.infra.research.history_control_repository import SQLiteHistoryControlRepository
from trader.infra.research.history_sync_runtime import run_history_sync
from trader.infra.research.history_training_due import evaluate_history_training_due
from trader.infra.research.history_training_input import SQLiteHistoryTrainingInputArchive

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


def test_monthly_training_input_binds_active_snapshot_and_reads_typed_rows(tmp_path: Path) -> None:
    archive_root = tmp_path / "history" / "baostock"
    configuration = HistorySyncConfiguration(archive_root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    result = run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW)

    assert result.state == "completed"
    archive = SQLiteHistoryTrainingInputArchive.open(tmp_path / "history")
    rows = archive.read_training_rows("600001", allowed_dates=frozenset(dates))

    assert archive.snapshot.input_hash == SQLiteHistoryControlRepository(
        archive_root / "control.sqlite3"
    ).load_state().active_snapshot_hash
    assert tuple(row.trade_date for row in rows) == dates
    assert archive.snapshot.label_cutoff == dates[-2]


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
