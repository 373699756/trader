"""Single Tencent daily wire protocol, including proven neutral adjustments."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from trader.infra.http_contracts import HttpTransport

_ENDPOINTS = {
    "proxy": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "direct": "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
}


@dataclass(frozen=True)
class TencentDailyQuery:
    symbol: str
    start: date
    end: date
    mode: Literal["bfq", "qfq"]
    count: int


@dataclass(frozen=True)
class TencentDailyRow:
    day: date
    open_price: float | None
    close: float | None
    high: float | None
    low: float | None
    volume: float | None
    amount: float | None
    turnover_rate: float | None
    turnover_valid: bool

    def prices(self) -> tuple[float, float, float, float]:
        if self.open_price is None or self.close is None or self.high is None or self.low is None:
            raise ValueError("tencent_daily_row_fields_missing")
        return self.open_price, self.close, self.high, self.low


def TencentDailyRequest(query: TencentDailyQuery, host: str) -> tuple[str, dict[str, str]]:
    if host not in _ENDPOINTS:
        raise ValueError("Tencent history host must be proxy or direct")
    return _ENDPOINTS[host], {
        "_var": f"kline_day{query.mode}{query.end.year}",
        "param": f"{query.symbol},day,{query.start.isoformat()},{query.end.isoformat()},{query.count},{query.mode}",
    }


def DecodeTencentDailyPayload(text: str) -> object:
    marker = text.find("={")
    return json.loads(text[marker + 1 :] if marker >= 0 else text)


def FetchTencentDailyRows(
    transport: HttpTransport,
    query: TencentDailyQuery,
    *,
    host: str,
    timeout_seconds: float,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[TencentDailyRow, ...]:
    url, params = TencentDailyRequest(query, host)
    response = transport.get(
        url,
        params=params,
        timeout=timeout_seconds,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        cancel_requested=cancel_requested,
    )
    response.raise_for_status()
    return DecodeTencentDailyRows(response.text, query)


def DecodeTencentDailyRows(text: str, query: TencentDailyQuery) -> tuple[TencentDailyRow, ...]:
    return tuple(_row(row) for row in _rows(DecodeTencentDailyPayload(text), query))


def _rows(payload: object, query: TencentDailyQuery) -> list[object]:
    if not isinstance(payload, Mapping) or payload.get("code", 0) != 0:
        raise ValueError("tencent_daily_response_rejected")
    data = payload.get("data")
    stock = data.get(query.symbol) if isinstance(data, Mapping) else None
    if not isinstance(stock, Mapping):
        raise ValueError("tencent_daily_stock_data_missing")
    rows = stock.get("qfqday" if query.mode == "qfq" else "day")
    if query.mode == "qfq" and rows is None:
        neutral = stock.get("day")
        if isinstance(neutral, list) and neutral and all(_neutral_adjustment(row) for row in neutral):
            rows = neutral
        elif neutral is not None:
            raise ValueError("tencent_daily_qualified_rows_missing")
    if not isinstance(rows, list) or len(rows) > query.count + 1:
        raise ValueError("tencent_daily_response_shape_invalid")
    return rows


def _neutral_adjustment(row: object) -> bool:
    return (
        isinstance(row, list)
        and len(row) >= 11
        and isinstance(row[6], Mapping)
        and not row[6]
        and _finite_zero(row[9])
        and _finite_zero(row[10])
    )


def _finite_zero(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return False
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number.is_zero()


def _number(value: object, scale: float = 1.0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value) * scale
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _row(value: object) -> TencentDailyRow:
    if not isinstance(value, list) or not value or not isinstance(value[0], str):
        raise ValueError("tencent_daily_row_invalid")
    day = date.fromisoformat(value[0])
    if len(value) < 9:
        return TencentDailyRow(day, None, None, None, None, None, None, None, False)
    turnover = _number(value[7])
    return TencentDailyRow(
        day,
        _number(value[1]),
        _number(value[2]),
        _number(value[3]),
        _number(value[4]),
        _number(value[5], 100.0),
        _number(value[8], 10000.0),
        turnover,
        value[7] in (None, "") or turnover is not None,
    )


__all__ = [
    "DecodeTencentDailyPayload",
    "DecodeTencentDailyRows",
    "FetchTencentDailyRows",
    "TencentDailyQuery",
    "TencentDailyRequest",
    "TencentDailyRow",
]
