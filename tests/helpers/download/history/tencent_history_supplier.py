from __future__ import annotations
import json
from datetime import date, timedelta
from tests.helpers.download.qfq import QFQ_TENCENT_OPTIONS
from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.infra.tencent_qfq_supplier import TencentQfqDependencies, TencentQfqSupplier
from trader.infra.http_contracts import HttpTimeout

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
        return TencentQfqSupplier(
            TencentQfqDependencies(lambda: self, lambda _as_of: (SECURITY,), lambda: self.cancelled),
            QFQ_TENCENT_OPTIONS,
        )


class _PagedHttp(_Http):
    def __init__(self, days, *, conflicting_anchor=False):
        super().__init__()
        self.raw = [[day.isoformat(), *ROW[1:]] for day in days]
        self.adjusted = self.raw
        self.conflicting_anchor = conflicting_anchor

    def get(self, url, **kwargs):
        param = kwargs["params"]["param"]
        if not param.endswith(",qfq"):
            return super().get(url, **kwargs)
        self.calls.append(param)
        end = date.fromisoformat(param.split(",")[3])
        rows = [row.copy() for row in self.adjusted if date.fromisoformat(row[0]) <= end][-640:]
        if self.conflicting_anchor and end < DAY and rows:
            rows[-1][2] = "11"
        return _Response({"code": 0, "data": {"sh600001": {"qfqday": rows}}})


class _SuspensionHttp(_Http):
    def __init__(self):
        super().__init__()
        self.raw = self.adjusted = []
        self.quote = [""] * 88
        self.quote[2] = SECURITY.code
        self.quote[3] = "10.00"  # A suspended quote can still carry the last traded price.
        self.quote[30] = DAY.strftime("%Y%m%d") + "161500"
        self.quote[40] = "S"
        for index in (5, 6, 33, 34, 37):
            self.quote[index] = "0"
        self.fail_qfq = False

    def get(self, url, **kwargs):
        if self.fail_qfq and kwargs["params"]["param"].endswith(",qfq"):
            raise HttpTimeout("fixture")
        response = super().get(url, **kwargs)
        payload = json.loads(response.text[2:])
        symbol = kwargs["params"]["param"].split(",")[0]
        payload["data"][symbol]["qt"] = {symbol: self.quote}
        return _Response(payload)
