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
from trader.infra.research.history_archive_status import inspect_history_archive
from trader.infra.research.history_sync_runtime import run_history_sync

NOW = datetime(2026, 9, 10, 20, 30, tzinfo=ZoneInfo("Asia/Shanghai"))


def _side(day: date, adjustment: str) -> BaoStockDailySide:
    return BaoStockDailySide(
        "600001",
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

    def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        return HistorySupplierContext(
            BaoStockCalendar(self._dates),
            (BaoStockSecurity("600001", "样本", "main", date(2000, 1, 1), None, "test"),),
            BaoStockSourceVersions("test", "3.12", ()),
            (BaoStockIndustryInterval("600001", self._dates[0], None, "银行", "申万"),),
        )

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        cells = tuple(
            BaoStockDailyCell(
                security.code,
                day,
                "complete",
                _side(day, "unadjusted"),
                _side(day, "qfq"),
            )
            for day in dates
        )
        return BaoStockCodeDownload(
            BaoStockCodeBatch(security.code, cells),
            tuple(BaoStockDailyFact(security.code, day, False) for day in dates),
        )


def test_missing_archive_is_read_only_and_reports_the_current_snapshot_boundary(tmp_path: Path) -> None:
    root = tmp_path / "history"

    status = inspect_history_archive(root)

    assert status.state == "unavailable"
    assert status.reason == "history_snapshot_unavailable"
    assert status.active_snapshot_hash is None
    assert status.production_authority is False
    assert not root.exists()


def test_active_monthly_snapshot_projects_typed_counts_and_optional_full_verification(tmp_path: Path) -> None:
    root = tmp_path / "history" / "baostock"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    configuration = HistorySyncConfiguration(root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW).state == "completed"

    status = inspect_history_archive(tmp_path / "history", verify_partitions=True)

    assert status.state == "active"
    assert status.calendar_sessions == 3
    assert status.universe_count == 1
    assert status.partition_count == 1
    assert status.data_cutoff == dates[-1]
    assert status.label_cutoff == dates[-2]
    assert status.reason is None
    assert len(status.active_snapshot_hash or "") == 64


def test_partition_tamper_fails_closed_without_losing_the_snapshot_identity(tmp_path: Path) -> None:
    root = tmp_path / "history" / "baostock"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    configuration = HistorySyncConfiguration(root, sessions=3, reread_sessions=2, minimum_free_bytes=0)
    assert run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW).state == "completed"
    active = inspect_history_archive(root)
    partition = next((root / "partitions").glob("*/*.sqlite3"))
    with partition.open("ab") as handle:
        handle.write(b"tamper")

    verified = inspect_history_archive(root, verify_partitions=True)

    assert active.state == "active"
    assert verified.state == "invalid"
    assert verified.reason == "history_snapshot_partition_invalid"
    assert verified.active_snapshot_hash == active.active_snapshot_hash
