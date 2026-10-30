"""Tencent bounded responses and transport failure contracts."""

import json
from dataclasses import replace
from datetime import date, timedelta

import pytest

from tests.helpers.qfq import QFQ_TENCENT_OPTIONS
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqSupplier
from trader.infra.http_contracts import HttpTimeout

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
        older_page = mode == "qfq" and end < _DAYS[-1].isoformat()
        if self.behavior == "offline" or (older_page and self.behavior == "partial_timeout"):
            raise HttpTimeout()
        if self.behavior == "alternate" and "proxy.finance" in url:
            raise HttpTimeout()
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
        if older_page and self.behavior == "invalid_old":
            rows[-1][0] = rows[-2][0]
        return _Response({"qfqday" if mode == "qfq" else "day": rows})

    def supplier(self):
        return TencentQfqSupplier(
            TencentQfqDependencies(lambda: self, lambda _day: (_SECURITY,), lambda: self.cancelled),
            replace(QFQ_TENCENT_OPTIONS, retries=0),
        )


def _fetch(http, days=_DAYS):
    return http.supplier().fetch_window(_SECURITY, days, request_count=2000, allow_interior_padding=True)


def test_qfq_pages_fill_full_history_without_filling_nonexistent_days():
    http = _Session()
    window = _fetch(http)
    assert len(window.cells) == 2000
    assert all(cell.status == "complete" for cell in window.cells)
    assert sum(call[-1] == "bfq" for call in http.calls) == 1
    assert len(http.calls) == 5
    assert all("proxy.finance" in call[0] for call in http.calls)


def test_invalid_older_padding_cannot_discard_valid_requested_prices():
    http = _Session("bad_padding")
    window = _fetch(http, _DAYS[-251:])
    assert len(window.cells) == 251
    assert all(cell.status == "complete" for cell in window.cells)
    assert len(http.calls) == 2


def test_sparse_old_dates_can_cross_pages_without_repeating_one_boundary():
    http = _Session()
    days = _DAYS[:2] + _DAYS[-5:]
    window = _fetch(http, days)
    assert tuple(cell.trade_date for cell in window.cells) == days
    assert all(cell.status == "complete" for cell in window.cells)
    assert len(http.calls) == 5


def test_older_page_timeout_preserves_recent_pairs_as_pending():
    http = _Session("partial_timeout")
    with pytest.raises(HistorySupplierPendingError) as pending:
        _fetch(http)
    window = pending.value.window
    assert sum(cell.status == "complete" for cell in window.cells) == 640
    assert len(http.calls) == 5


def test_primary_transport_failure_uses_alternate_before_giving_up():
    http = _Session("alternate")
    assert all(cell.status == "complete" for cell in _fetch(http, _DAYS[-251:]).cells)
    assert len(http.calls) == 4
    assert sum("web.ifzq" in call[0] for call in http.calls) == 2


def test_successful_empty_qfq_response_keeps_actual_pair_count():
    http = _Session("unavailable")
    window = _fetch(http)
    assert sum(cell.status == "complete" for cell in window.cells) == 0
    assert len(http.calls) == 2


def test_conflicting_older_page_preserves_recent_pairs_as_pending():
    http = _Session("conflict")
    with pytest.raises(HistorySupplierPendingError) as pending:
        _fetch(http)
    assert sum(cell.status == "complete" for cell in pending.value.window.cells) == 640
    assert len(http.calls) == 3
    assert all("proxy.finance" in call[0] for call in http.calls)


def test_duplicate_older_page_keeps_recent_pairs_pending_without_switching_hosts():
    http = _Session("invalid_old")
    with pytest.raises(HistorySupplierPendingError) as pending:
        _fetch(http)
    assert sum(cell.status == "complete" for cell in pending.value.window.cells) == 640
    assert len(http.calls) == 3
    assert all("proxy.finance" in call[0] for call in http.calls)


def test_offline_stock_stops_after_two_hosts_and_cancellation_stops_before_io():
    http = _Session("offline")
    with pytest.raises(HistorySupplierPendingError):
        _fetch(http)
    assert len(http.calls) == 2
    http.cancelled = True
    with pytest.raises(RuntimeError, match="cancelled"):
        _fetch(http)
    assert len(http.calls) == 2
