import json
from argparse import Namespace
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from scripts.runtime_diagnostics import tencent_download


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
        Namespace(codes=("600001",), timeout_seconds=1, tencent_history_host="proxy")
    )
    assert result == 0
    assert reports[0]["status"] == status
    assert reports[0]["summary"]["samples"][0]["missing_sessions"] == 2000 - returned


def test_response_metadata_counts_nonpositive_qfq_without_exposing_prices():
    rows = [
        ["2026-01-01", "-1", "-1", "0", "-2"],
        ["2026-01-02", "1", "1", "1", "1"],
    ]
    response = SimpleNamespace(
        url="https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get?param=sz000937,day,2026-01-01,2026-01-02,2,qfq",
        status_code=200,
        text="v=" + json.dumps({"data": {"sz000937": {"qfqday": rows}}}),
    )
    observations = []

    tencent_download._observe_history_response(observations, response)

    assert observations[0]["nonpositive_ohlc_rows"] == 1
    assert observations[0]["first_nonpositive"] == "2026-01-01"
    assert "-1" not in str(observations[0])
