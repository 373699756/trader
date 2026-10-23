from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tests.reference_fixtures import PreparedFixture
from trader.download.application.sync_data import SyncData
from trader.download.domain.company_evidence import CompanyEvidenceSyncResult
from trader.download.domain.history_maintenance import HistoryMaintenanceStatus
from trader.download.domain.qfq_window import QfqUpdateResult
from trader.download.entrypoints.sync_projection import ProjectSyncResult

_NOW = datetime(2026, 10, 16, tzinfo=ZoneInfo("Asia/Shanghai"))


def _history(state="completed", unresolved_price_cells=0):
    return HistoryMaintenanceStatus(
        state, None, Path("history"), "tencent", None, "a" * 64, _NOW.date(), _NOW.date(), unresolved_price_cells
    )


@pytest.mark.parametrize("target", ("all", "history", "market", "company"))
def test_one_prepared_full_reference_is_shared_in_order(target):
    reference = PreparedFixture(_NOW)
    calls = []

    def prepare():
        calls.append("prepare")
        return reference

    def history(value):
        assert value is reference
        calls.append("history")
        return _history()

    def market(value):
        assert value is reference
        calls.append("market")
        return QfqUpdateResult(_NOW.date(), 1)

    def company(value):
        assert value is reference
        calls.append("company")
        return CompanyEvidenceSyncResult(_NOW, 1, 1, 0, 0, 0, 0, (), 0)

    result = SyncData(prepare, history, market, company, lambda: False).execute(target)
    assert calls == ["prepare", *(("history", "market", "company") if target == "all" else (target,))]
    assert result.exit_code == 0
    assert ProjectSyncResult(result)["status"] == "completed"


@pytest.mark.parametrize("pending", (False, True))
def test_failed_or_incomplete_history_continues_market_and_company(pending):
    calls = []

    def history(value):
        if pending:
            return _history(unresolved_price_cells=1)
        raise RuntimeError("history unavailable")

    def market(value):
        calls.append("market")
        return QfqUpdateResult(_NOW.date(), 1)

    def company(value):
        calls.append("company")
        return CompanyEvidenceSyncResult(_NOW, 1, 1, 0, 0, 0, 0, (), 0)

    result = SyncData(lambda: PreparedFixture(_NOW), history, market, company, lambda: False).execute("all")
    assert calls == ["market", "company"]
    assert result.exit_code == 1
    assert result.tasks[0].status == ("pending" if pending else "failed")


@pytest.mark.parametrize("target", ("all", "history", "market", "company"))
def test_common_preparation_failure_stops_all_tasks(target):
    def fail():
        raise ValueError("invalid official population")

    def forbidden(value):
        pytest.fail("dependent task ran after preparation failed")

    result = SyncData(fail, forbidden, forbidden, forbidden, lambda: False).execute(target)
    assert result.tasks == () and result.exit_code == 1


@pytest.mark.parametrize("interrupt", (False, True))
def test_cancellation_stops_following_tasks(interrupt):
    def history(value):
        if interrupt:
            raise KeyboardInterrupt
        return _history(state="cancelled")

    def forbidden(value):
        pytest.fail("task ran after cancellation")

    result = SyncData(lambda: PreparedFixture(_NOW), history, forbidden, forbidden, lambda: False).execute("all")
    assert result.exit_code == 130 and result.cancelled


def test_cancel_before_preparation_has_no_side_effects():
    def forbidden(*args):
        pytest.fail("cancelled work ran")

    assert SyncData(forbidden, forbidden, forbidden, forbidden, lambda: True).execute("all").exit_code == 130
