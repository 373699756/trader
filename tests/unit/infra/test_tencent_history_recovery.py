"""Tencent bounded responses and transport failure contracts."""

import json
from datetime import date, timedelta

import pytest
import requests

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqOptions, TencentQfqSupplier

_DAYS = tuple(date(2018, 1, 1) + timedelta(days=i) for i in range(2800) if i % 7 < 5)
_SECURITY = BaoStockSecurity("600001", "fixture", "main", _DAYS[0], None, "fixture")


class _Response:
    def __init__(self, stock):
        self.text = json.dumps({"code": 0, "data": {"sh600001": stock}})

    def raise_for_status(self):
        pass

    def close(self):
        pass


class _Session:
    def __init__(self, behavior="complete"):
        self.behavior = behavior
        self.calls = []
        self.cancelled = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def get(self, url, **kwargs):
        _, _, start, end, count, mode = kwargs["params"]["param"].split(",")
        self.calls.append((url, start, end, int(count), mode))
        older_page = mode == "qfq" and int(count) == 640
        if self.behavior == "offline" or older_page and self.behavior == "partial_timeout":
            raise requests.Timeout()
        if self.behavior == "alternate" and "proxy.finance" in url:
            raise requests.Timeout()
        days = tuple(day for day in _DAYS if day <= date.fromisoformat(end))
        days = days[-(min(int(count), 640) if mode == "qfq" else int(count)) :]
        if self.behavior == "unavailable" and mode == "qfq":
            days = ()
        rows = []
        for day in days:
            price = 11 if older_page and self.behavior == "conflict" else 10
            if self.behavior == "bad_padding" and day < date.fromisoformat(start):
                price = -10
            rows.append([day.isoformat(), price, price, price, price, 100, {}, "1", 20, "0", "0"])
        return _Response({"qfqday" if mode == "qfq" else "day": rows})

    def supplier(self):
        return TencentQfqSupplier(
            TencentQfqDependencies(lambda: self, lambda _day: (_SECURITY,), lambda: self.cancelled),
            TencentQfqOptions(retries=0),
        )


def _fetch(http, days=_DAYS):
    return http.supplier().fetch_window(_SECURITY, days, request_count=2000, allow_interior_padding=True)


def test_truncated_qfq_keeps_only_returned_pairs_without_extra_requests():
    http = _Session()
    window = _fetch(http)
    assert len(window.cells) == 2000
    assert sum(cell.status == "complete" for cell in window.cells) == 640
    assert sum(cell.status == "qfq_missing" for cell in window.cells) == 1360
    assert sum(call[-1] == "bfq" for call in http.calls) == 1
    assert len(http.calls) == 2
    assert all("proxy.finance" in call[0] for call in http.calls)


def test_invalid_older_padding_cannot_discard_valid_requested_prices():
    http = _Session("bad_padding")
    window = _fetch(http, _DAYS[-251:])
    assert len(window.cells) == 251
    assert all(cell.status == "complete" for cell in window.cells)
    assert len(http.calls) == 2


def test_sparse_old_dates_keep_only_the_returned_qfq_rows():
    http = _Session()
    days = _DAYS[:2] + _DAYS[-5:]
    window = _fetch(http, days)
    assert tuple(cell.trade_date for cell in window.cells) == days
    assert tuple(cell.status for cell in window.cells) == ("qfq_missing", "qfq_missing", *(("complete",) * 5))
    assert len(http.calls) == 2


def test_short_success_is_not_retried_as_a_transport_failure():
    http = _Session("partial_timeout")
    window = _fetch(http)
    assert sum(cell.status == "complete" for cell in window.cells) == 640
    assert len(http.calls) == 2


def test_primary_transport_failure_uses_alternate_before_giving_up():
    http = _Session("alternate")
    assert all(cell.status == "complete" for cell in _fetch(http, _DAYS[-251:]).cells)
    assert len(http.calls) == 4
    assert sum("web.ifzq" in call[0] for call in http.calls) == 2


@pytest.mark.parametrize("behavior, paired", (("unavailable", 0), ("conflict", 640)))
def test_successful_short_responses_keep_the_returned_pair_count(behavior, paired):
    http = _Session(behavior)
    window = _fetch(http)
    assert sum(cell.status == "complete" for cell in window.cells) == paired
    assert len(http.calls) == 2


def test_offline_stock_stops_after_two_hosts_and_cancellation_stops_before_io():
    http = _Session("offline")
    with pytest.raises(HistorySupplierPendingError):
        _fetch(http)
    assert len(http.calls) == 2
    http.cancelled = True
    with pytest.raises(RuntimeError, match="cancelled"):
        _fetch(http)
    assert len(http.calls) == 2
