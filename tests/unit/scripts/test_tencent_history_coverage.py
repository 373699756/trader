from argparse import Namespace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from scripts.runtime_diagnostics import tencent_download
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_trading_status import HistoryTradingFact


@pytest.mark.parametrize("returned,status", ((640, "degraded"), (0, "degraded"), (2000, "passed")))
def test_real_calendar_probe_does_not_call_a_short_response_passed(monkeypatch, returned, status):
    days = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2000))

    class Supplier:
        def __init__(self, *_args):
            pass

        def load_calendar(self, _end, sessions):
            assert sessions == 2000
            return SimpleNamespace(open_dates=days)

        def fetch_window(self, _security, dates, **_kwargs):
            assert dates == days
            return SimpleNamespace(
                cells=tuple(
                    SimpleNamespace(status="complete", trade_date=day, unadjusted=True, qfq=True)
                    for day in days[-returned:]
                    if returned
                )
            )

    reports = []
    monkeypatch.setattr(tencent_download, "TencentQfqSupplier", Supplier)
    monkeypatch.setattr(tencent_download, "emit_report", reports.append)
    result = tencent_download._whole_window_probe(
        Namespace(codes=("600001",), timeout_seconds=1, tencent_history_host="proxy", verify_trading_status=False)
    )
    assert result == 0
    assert reports[0]["status"] == status
    assert reports[0]["summary"]["samples"][0]["missing_sessions"] == 2000 - returned


def test_status_probe_is_explicit_and_checks_at_most_twenty_missing_dates(monkeypatch):
    calls = []

    class StatusSupplier:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def fetch_trading_facts(self, security, dates):
            calls.append(dates)
            return tuple(HistoryTradingFact(security.code, day, "suspended", False) for day in dates)

    monkeypatch.setattr(tencent_download, "BaoStockHistorySupplier", StatusSupplier)
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    dates = tuple(date(2020, 1, 1) + timedelta(days=i) for i in range(30))
    args = Namespace(verify_trading_status=False, timeout_seconds=5)
    assert tencent_download._probe_gap_status(args, security, dates) == {} and not calls
    args.verify_trading_status = True
    report = tencent_download._probe_gap_status(args, security, dates)
    assert calls == [dates[:20]]
    assert report["confirmed_suspended_sessions"] == 20
    assert report["unverified_status_sessions"] == 10
