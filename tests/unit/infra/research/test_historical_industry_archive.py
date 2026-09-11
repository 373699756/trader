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
from trader.infra.research.historical_industry_archive import audit_archived_historical_industry_facts
from trader.infra.research.history_archive_sync import run_history_sync

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
        self._universe = tuple(
            BaoStockSecurity(
                f"{index + 1:06d}",
                f"S{index}",
                ("main", "chinext", "star")[index % 3],  # type: ignore[arg-type]
                dates[0],
                None,
                "fixture",
            )
            for index in range(300)
        )

    def load_context(self, _as_of: date, _sessions: int) -> HistorySupplierContext:
        intervals = tuple(
            BaoStockIndustryInterval(item.code, self._dates[0], None, "银行", "申万一级行业") for item in self._universe
        )
        return HistorySupplierContext(
            BaoStockCalendar(self._dates),
            self._universe,
            BaoStockSourceVersions("00.9.30", "3.14.0", ()),
            intervals,
        )

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
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


def test_monthly_source_audit_covers_three_boards_and_keeps_missing_query_time_fail_closed(
    tmp_path: Path,
) -> None:
    dates = (date(2026, 8, 28), date(2026, 8, 31))
    root = tmp_path / "history" / "baostock"
    configuration = HistorySyncConfiguration(root, sessions=2, reread_sessions=1, minimum_free_bytes=0)
    assert run_history_sync(configuration, _Supplier(dates), clock=lambda: NOW).state == "completed"

    report = audit_archived_historical_industry_facts(tmp_path / "history", tushare_access_points=120)

    baostock, tushare = report.sources
    assert report.daily_manifest_hash == baostock.contract.source_version
    assert baostock.source == "baostock_archived_industry"
    assert baostock.sampled_codes == 300
    assert baostock.coverage_ratio == 1.0
    assert baostock.board_counts == (("chinext", 100), ("main", 100), ("star", 100))
    assert baostock.status == "historical_data_insufficient"
    assert "industry_query_time_unavailable" in baostock.failure_reasons
    assert tushare.source == "tushare_index_member_all"
    assert "source_access_below_required_points" in tushare.failure_reasons
    assert report.production_authority is False
