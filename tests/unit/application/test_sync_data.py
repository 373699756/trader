from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.download.application.sync_data import SyncData
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.entrypoints.sync_projection import ProjectSyncResult


def test_common_preparation_once_and_independent_failure_continues():
    population = (BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture"),)
    calls = []

    def prepare():
        calls.append("prepare")
        return population

    def market(value):
        assert value is population
        calls.append("market")
        raise RuntimeError("supplier failed")

    def company(value):
        assert value is population
        calls.append("company")
        return CompanyEvidenceSyncResult(
            datetime(2026, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai")), 1, 1, 0, 0, 0, 0, (), 0.0
        )

    result = SyncData(prepare, market, company, lambda: False).execute("all")
    assert calls == ["prepare", "market", "company"]
    assert result.exit_code == 1
    assert tuple(task.status for task in result.tasks) == ("failed", "completed")
    assert ProjectSyncResult(result)["status"] == "failed"


@pytest.mark.parametrize("target", ("market", "company"))
def test_common_preparation_failure_stops_all_tasks(target):
    def fail():
        raise ValueError("invalid official population")

    def forbidden(_population):
        pytest.fail("task ran after common preparation failed")

    result = SyncData(fail, forbidden, forbidden, lambda: False).execute(target)
    assert result.tasks == () and result.exit_code == 1
    assert ProjectSyncResult(result)["status"] == "failed"


def test_cancellation_after_market_stops_company():
    population = (BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture"),)
    cancelled = False

    def market(_population):
        nonlocal cancelled
        cancelled = True
        return QfqUpdateResult(date(2026, 10, 16), 1)

    def company(_population):
        pytest.fail("company started after cancellation")

    result = SyncData(lambda: population, market, company, lambda: cancelled).execute("all")
    assert result.exit_code == 130 and result.cancelled
    assert len(result.tasks) == 1


def test_cancel_before_preparation_has_no_side_effects():
    def forbidden(*_args):
        pytest.fail("cancelled work ran")

    assert SyncData(forbidden, forbidden, forbidden, lambda: True).execute("all").exit_code == 130
