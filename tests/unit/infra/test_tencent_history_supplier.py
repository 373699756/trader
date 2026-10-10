from __future__ import annotations

import json
from datetime import date, timedelta

import pytest
import requests

from trader.download.domain.baostock_daily import BaoStockDailySide, BaoStockSecurity
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqOptions, TencentQfqSupplier

DAY = date(2026, 10, 9)
SECURITY = BaoStockSecurity("600001", "fixture", "main", date(2020, 1, 1), None, "fixture")
ROW = [DAY.isoformat(), "10", "10.5", "11", "9.5", "100", {}, "1.2", "20", "0", "0"]


class _Response:
    def __init__(self, payload):
        self.text = "v=" + json.dumps(payload)

    def raise_for_status(self):
        pass

    def close(self):
        pass


class _Http:
    def __init__(self):
        self.raw = [ROW.copy()]
        self.adjusted = [ROW.copy()]
        self.calendar = [[(DAY - timedelta(days=offset)).isoformat()] for offset in reversed(range(251))]
        self.calls = []
        self.cancelled = False
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def get(self, _url, **kwargs):
        param = kwargs["params"]["param"]
        self.calls.append(param)
        if self.error:
            raise self.error
        symbol = param.split(",")[0]
        if symbol == "sh000001":
            stock = {"day": self.calendar}
        else:
            stock = (
                self.adjusted
                if param.endswith(",qfq") and isinstance(self.adjusted, dict)
                else {
                    "qfqday" if param.endswith(",qfq") else "day": self.adjusted if param.endswith(",qfq") else self.raw
                }
            )
        return _Response({"code": 0, "data": {symbol: stock}})

    def supplier(self):
        return TencentQfqSupplier(TencentQfqDependencies(lambda: self, lambda: (SECURITY,), lambda: self.cancelled))


def test_pairs_preserve_units_and_leave_unavailable_facts_missing():
    window = _Http().supplier().fetch_window(SECURITY, (DAY,))
    cell = window.cells[0]
    assert cell.status == "complete"
    assert cell.unadjusted.volume == 10000
    assert cell.unadjusted.amount == 200000
    assert cell.unadjusted.turnover == 1.2
    assert cell.unadjusted.preclose is None and cell.unadjusted.pct_change is None
    assert cell.qfq.turnover is None
    with pytest.raises(ValueError, match="requires preclose"):
        BaoStockDailySide("600001", DAY, "unadjusted", 10, 11, 9, 10, 100, 200, None, None, 1, "trading")


def test_calendar_advances_independently_of_old_history():
    context = _Http().supplier().load_qfq_context(DAY, 251)
    assert context.calendar.open_dates[-1] == DAY
    assert context.universe == (SECURITY,)


@pytest.mark.parametrize("invalid", ("duplicate", "future", "short", "nan", "range"))
def test_rejects_invalid_rows_without_silently_dropping_them(invalid):
    http = _Http()
    if invalid == "duplicate":
        http.raw.append(ROW.copy())
    elif invalid == "future":
        http.raw[0][0] = (DAY + timedelta(days=1)).isoformat()
    elif invalid == "short":
        http.raw = [[DAY.isoformat()]]
    elif invalid == "nan":
        http.raw[0][5] = "nan"
    else:
        http.raw[0][3] = "9"
    with pytest.raises(ValueError):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 1


def test_missing_one_side_remains_pending_and_is_not_suspended():
    http = _Http()
    http.adjusted = []
    cell = http.supplier().fetch_window(SECURITY, (DAY,)).cells[0]
    assert cell.status == "qfq_missing" and cell.qfq is None


def test_supplier_older_padding_is_trimmed_without_accepting_future_rows():
    http = _Http()
    older = ROW.copy()
    older[0] = (DAY - timedelta(days=1)).isoformat()
    http.raw.insert(0, older)
    http.adjusted.insert(0, older)
    assert len(http.supplier().fetch_window(SECURITY, (DAY,)).cells) == 1


@pytest.mark.parametrize("zero", ("0", "0.0", "0.00", "-0.00", "0e-9", 0, 0.0))
def test_neutral_adjustment_accepts_numeric_zero_evidence(zero):
    http = _Http()
    http.adjusted = {"day": [ROW.copy()]}
    http.adjusted["day"][0][9:11] = [zero, zero]
    cell = http.supplier().fetch_window(SECURITY, (DAY,)).cells[0]
    assert cell.status == "complete"
    assert cell.unadjusted.close_price == cell.qfq.close_price == 10.5
    assert cell.qfq.adjustment == "qfq"


@pytest.mark.parametrize("index", (9, 10))
@pytest.mark.parametrize("invalid", ("1", "-0.01", "1e-9999", "nan", "inf", "-inf", "bad", "", None, False, {}, []))
def test_neutral_adjustment_rejects_invalid_or_nonzero_evidence(index, invalid):
    http = _Http()
    http.adjusted = {"day": [ROW.copy()]}
    http.adjusted["day"][0][index] = invalid
    with pytest.raises(ValueError, match="qualified_rows"):
        http.supplier().fetch_window(SECURITY, (DAY,))


@pytest.mark.parametrize("invalid", ("short", "adjustment_record", "mixed_rows"))
def test_neutral_adjustment_requires_evidence_on_every_row(invalid):
    http = _Http()
    row = ROW.copy()
    row[9:11] = ["0.00", "0.00"]
    http.adjusted = {"day": [row]}
    if invalid == "short":
        del row[10:]
    elif invalid == "adjustment_record":
        row[6] = {"fixture": "adjusted"}
    else:
        changed = row.copy()
        changed[9] = "1"
        http.adjusted["day"].append(changed)
    with pytest.raises(ValueError, match="qualified_rows"):
        http.supplier().fetch_window(SECURITY, (DAY,))


def test_retries_transport_only_and_cancellation_stops_attempts():
    http = _Http()
    http.error = requests.Timeout()
    with pytest.raises(RuntimeError, match="request_failed"):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 3
    http.cancelled = True
    with pytest.raises(RuntimeError, match="cancelled"):
        http.supplier().fetch_window(SECURITY, (DAY,))
    assert len(http.calls) == 3


def test_rejects_unbounded_or_nonfinite_options():
    for kwargs in ({"retries": 3}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 0}):
        with pytest.raises(ValueError):
            TencentQfqOptions(**kwargs)
