from __future__ import annotations

from tests.helpers.configuration import SqlitePolicyFixture

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tests.helpers.history import RunHistoryFixture
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
from trader.download.domain.history_sync import HistorySupplierContext
from tests.helpers.configuration import HistorySyncConfigurationFixture as HistorySyncConfiguration
from trader.download.domain.published_history import ProjectHistoryCell, PublishedHistoryWindow
from trader.download.infra.history_snapshot_inspection import InspectHistorySnapshot
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

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
            (HistoryStEvidence("600001", _as_of, "clear"),),
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

    def fetch_tencent_window(self, security, dates):
        return PublishedHistoryWindow(
            security.code, tuple(ProjectHistoryCell(cell) for cell in self.fetch_code(security, dates).batch.cells)
        )

    def fetch_baostock_raw(self, security, dates):
        return self.fetch_code(security, dates)

    def fetch_baostock_window(self, security, dates):
        raise RuntimeError("fixture_baostock_unavailable")


def test_missing_history_is_read_only_and_reports_the_current_snapshot_boundary(tmp_path: Path) -> None:
    root = tmp_path / "history"

    status = InspectHistorySnapshot(root, sqlite_policy=SqlitePolicyFixture())

    assert status.state == "unavailable"
    assert status.reason == "history_snapshot_unavailable"
    assert status.snapshot_hash is None
    assert status.production_authority is False
    assert not root.exists()


def test_published_monthly_snapshot_projects_typed_counts_and_optional_full_verification(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    configuration = HistorySyncConfiguration(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(root).parent / "reference"
    )
    assert RunHistoryFixture(configuration, _Supplier(dates), clock=lambda: NOW).state == "completed"

    status = InspectHistorySnapshot(root, verify_partitions=True, sqlite_policy=SqlitePolicyFixture())

    assert status.state == "published"
    assert status.calendar_sessions == 3
    assert status.universe_count == 1
    assert status.partition_count == 1
    assert status.data_cutoff == dates[-1]
    assert status.label_cutoff == dates[-2]
    assert status.reason is None
    assert len(status.snapshot_hash or "") == 64


def test_legacy_nested_history_is_not_discovered(tmp_path: Path) -> None:
    legacy_root = tmp_path / "history" / "baostock"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    configuration = HistorySyncConfiguration(
        legacy_root,
        sessions=3,
        reread_sessions=2,
        minimum_free_bytes=0,
        reference_root=(legacy_root).parent / "reference",
    )
    assert RunHistoryFixture(configuration, _Supplier(dates), clock=lambda: NOW).state == "completed"

    status = InspectHistorySnapshot(tmp_path / "history", sqlite_policy=SqlitePolicyFixture())

    assert status.state == "unavailable"
    assert status.reason == "history_snapshot_unavailable"


def test_partition_tamper_fails_closed_without_losing_the_snapshot_identity(tmp_path: Path) -> None:
    root = tmp_path / "history"
    dates = (date(2026, 9, 8), date(2026, 9, 9), date(2026, 9, 10))
    configuration = HistorySyncConfiguration(
        root, sessions=3, reread_sessions=2, minimum_free_bytes=0, reference_root=(root).parent / "reference"
    )
    assert RunHistoryFixture(configuration, _Supplier(dates), clock=lambda: NOW).state == "completed"
    current_snapshot = InspectHistorySnapshot(root, sqlite_policy=SqlitePolicyFixture())
    partition = next(root.glob("prices_*.sqlite3"))
    with SqliteSession(partition) as connection, SqliteTransaction(connection):
        connection.run("UPDATE daily_price_revision SET content_hash=?", (bytes(32),))

    verified = InspectHistorySnapshot(root, verify_partitions=True, sqlite_policy=SqlitePolicyFixture())

    assert current_snapshot.state == "published"
    assert verified.state == "invalid"
    assert verified.reason == "history_snapshot_partition_invalid"
    assert verified.snapshot_hash == current_snapshot.snapshot_hash
