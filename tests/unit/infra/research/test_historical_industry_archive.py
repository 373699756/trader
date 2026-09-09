from datetime import date
from pathlib import Path
from types import SimpleNamespace

import trader.infra.research.historical_industry_archive as adapter
from trader.application.research.baostock_daily import BaoStockShardContext
from trader.domain.research.baostock_daily import (
    BaoStockCalendar,
    BaoStockIndustryInterval,
    BaoStockSecurity,
    BaoStockSourceVersions,
)


def test_archived_source_audit_covers_three_boards_and_keeps_missing_query_time_fail_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calendar = BaoStockCalendar((date(2026, 8, 28), date(2026, 8, 31)))
    universe = tuple(
        BaoStockSecurity(
            f"{index + 1:06d}",
            f"S{index}",
            ("main", "chinext", "star")[index % 3],
            calendar.open_dates[0],
            None,
            "fixture",
        )
        for index in range(300)
    )
    intervals = tuple(
        BaoStockIndustryInterval(item.code, calendar.open_dates[0], None, "银行", "申万一级行业") for item in universe
    )
    context = BaoStockShardContext(
        calendar,
        universe,
        BaoStockSourceVersions("00.9.30", "3.14.0", ()),
        intervals,
    )
    manifest = SimpleNamespace(
        content_hash="a" * 64,
        partitions=(SimpleNamespace(relative_path="shards/main-0000.sqlite3"),),
    )

    class FakeArchive:
        def __init__(self, root: Path) -> None:
            assert root == tmp_path / "baostock-daily" / "sessions-2000"

        def verify(self):
            return manifest

    class FakeShard:
        def __init__(self, _path: Path) -> None:
            pass

        def context(self, _spec):
            return context

    monkeypatch.setattr(adapter, "BaoStockDailyPartitionedArchive", FakeArchive)
    monkeypatch.setattr(adapter, "SQLiteBaoStockDailyShard", FakeShard)

    report = adapter.audit_archived_historical_industry_facts(tmp_path, tushare_access_points=120)

    baostock, tushare = report.sources
    assert baostock.source == "baostock_archived_industry"
    assert baostock.sampled_codes == 300
    assert baostock.coverage_ratio == 1.0
    assert baostock.board_counts == (("chinext", 100), ("main", 100), ("star", 100))
    assert baostock.status == "historical_data_insufficient"
    assert "industry_query_time_unavailable" in baostock.failure_reasons
    assert tushare.source == "tushare_index_member_all"
    assert "source_access_below_required_points" in tushare.failure_reasons
    assert report.production_authority is False
