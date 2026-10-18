"""Supplier metadata queries; no wire fields escape into research consumers."""

from collections.abc import Mapping
from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from trader.download.application.history_source_capability import HistorySourceSample
from trader.download.infra.tencent_daily import DecodeTencentDailyRows, TencentDailyQuery, TencentDailyRequest
from trader.infra.http_contracts import HttpParameter, HttpResponse


class CapabilityHttp(Protocol):
    def get(self, url: str, *, params: Mapping[str, HttpParameter], timeout: float) -> HttpResponse: ...


class FreeHistorySourceCapability:
    def __init__(self, http: CapabilityHttp) -> None:
        self._http = http

    def daily_sample(self, code: str, cutoff: date, timeout: float) -> HistorySourceSample:
        symbol = ("sh" if code.startswith(("5", "6", "9")) else "sz") + code
        query = TencentDailyQuery(symbol, date(2015, 1, 1), cutoff, "qfq", 1600)
        url, params = TencentDailyRequest(query, "proxy")
        response = self._http.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        dates = tuple(row.day for row in DecodeTencentDailyRows(response.text, query))
        return HistorySourceSample(dates=dates, response_bytes=len(response.content))

    def minute_sample(self, code: str, anchor: date, timeout: float) -> HistorySourceSample:
        response = self._http.get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            params={
                "secid": ("1." if code.startswith(("5", "6", "9")) else "0.") + code,
                "klt": "1",
                "fqt": "0",
                "beg": anchor.strftime("%Y%m%d"),
                "end": anchor.strftime("%Y%m%d"),
                "lmt": "1000",
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("rc") != 0:
            raise ValueError("eastmoney_capability_response_rejected")
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("klines") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("eastmoney_capability_rows_missing")
        timestamps = tuple(
            datetime.fromisoformat(row.split(",", 1)[0]).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            for row in rows
            if isinstance(row, str)
        )
        return HistorySourceSample(timestamps=timestamps, response_bytes=len(response.content))


__all__ = ["CapabilityHttp", "FreeHistorySourceCapability"]
