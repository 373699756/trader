"""Tencent raw/qfq short windows; missing supplier facts remain missing."""

from __future__ import annotations

import json
import math
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal

import requests

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockSecurity,
    BaoStockSourceVersions,
    daily_cell_status,
)
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow

_ENDPOINTS = {
    "proxy": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "direct": "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
}
_PROXIES = {"http": "", "https": "", "all": ""}


@dataclass(frozen=True)
class TencentQfqOptions:
    timeout_seconds: float = 15.0
    retries: int = 2
    history_host: Literal["proxy", "direct"] = "proxy"

    def __post_init__(self) -> None:
        if (
            self.history_host not in _ENDPOINTS
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
            or not 0 <= self.retries <= 2
        ):
            raise ValueError("Tencent qfq supplier configuration is invalid")


@dataclass(frozen=True)
class TencentQfqDependencies:
    session_factory: Callable[[], requests.Session]
    load_universe: Callable[[date], tuple[BaoStockSecurity, ...]]
    cancel_requested: Callable[[], bool]


class TencentQfqSupplier:
    """Use injected HTTP sessions; the composition root owns all worker resources."""

    def __init__(self, dependencies: TencentQfqDependencies, options: TencentQfqOptions | None = None) -> None:
        self._dependencies = dependencies
        self._options = options or TencentQfqOptions()

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        if not 1 <= sessions <= 251:
            raise ValueError("Tencent qfq context is limited to 251 sessions")
        self._check_cancel()
        universe = self._dependencies.load_universe(as_of)
        if not universe or len({security.code for security in universe}) != len(universe):
            raise ValueError("Tencent qfq universe is empty or duplicated")
        rows = self._request_rows("sh000001", as_of - timedelta(days=640), as_of, "bfq")
        dates = tuple(_row_date(row) for row in rows)
        if dates != tuple(sorted(set(dates))) or any(day > as_of for day in dates) or len(dates) < sessions:
            raise ValueError("Tencent exchange calendar is incomplete or invalid")
        return HistorySupplierContext(
            BaoStockCalendar(dates[-sessions:]),
            tuple(sorted(universe, key=lambda security: security.code)),
            BaoStockSourceVersions(
                "tencent-newfqkline", platform.python_version(), (("host", self._options.history_host),)
            ),
        )

    def fetch_window(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> PublishedHistoryWindow:
        if not dates or len(dates) > 640 or dates != tuple(sorted(set(dates))):
            raise ValueError("Tencent window must contain 1..640 ordered unique dates")
        symbol = security.source_code.replace(".", "")
        raw = self._fetch_side(symbol, dates, "bfq")
        qfq = self._fetch_side(symbol, dates, "qfq")
        self._check_cancel()
        return PublishedHistoryWindow(
            security.code,
            tuple(
                PublishedHistoryCell(
                    security.code, day, daily_cell_status(raw.get(day), qfq.get(day)), raw.get(day), qfq.get(day)
                )
                for day in dates
            ),
        )

    def _fetch_side(
        self, symbol: str, dates: tuple[date, ...], mode: Literal["bfq", "qfq"]
    ) -> dict[date, PublishedHistorySide]:
        rows = self._request_rows(symbol, dates[0], dates[-1], mode)
        result: dict[date, PublishedHistorySide] = {}
        expected = frozenset(dates)
        seen: set[date] = set()
        for row in rows:
            side = _parse_side(symbol[2:], row, mode)
            if side.trade_date in seen or side.trade_date > dates[-1]:
                raise ValueError("tencent_daily_rows_outside_calendar_or_duplicated")
            seen.add(side.trade_date)
            # Tencent may return older padding despite the requested start date.
            # Only that documented direction may be trimmed; future/interior anomalies fail closed.
            if side.trade_date < dates[0]:
                continue
            if side.trade_date not in expected:
                raise ValueError("tencent_daily_rows_outside_calendar_or_duplicated")
            result[side.trade_date] = side
        return result

    def _request_rows(self, symbol: str, start: date, end: date, mode: Literal["bfq", "qfq"]) -> list[object]:
        for attempt in range(self._options.retries + 1):
            self._check_cancel()
            try:
                return self._request_once(symbol, start, end, mode)
            except (requests.RequestException, OSError) as exc:
                if attempt == self._options.retries:
                    raise RuntimeError("tencent_qfq_request_failed") from exc
        raise RuntimeError("tencent_qfq_request_failed")

    def _request_once(self, symbol: str, start: date, end: date, mode: Literal["bfq", "qfq"]) -> list[object]:
        with self._dependencies.session_factory() as session:
            response = session.get(
                _ENDPOINTS[self._options.history_host],
                params={
                    "_var": f"kline_day{mode}{end.year}",
                    "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,{mode}",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=self._options.timeout_seconds,
                proxies=_PROXIES,
            )
            try:
                response.raise_for_status()
                marker = response.text.find("={")
                payload: object = json.loads(response.text[marker + 1 :] if marker >= 0 else response.text)
            finally:
                response.close()
        self._check_cancel()
        return _payload_rows(payload, symbol, mode)

    def _check_cancel(self) -> None:
        if self._dependencies.cancel_requested():
            raise RuntimeError("tencent_qfq_cancelled")


def _payload_rows(payload: object, symbol: str, mode: str) -> list[object]:
    if not isinstance(payload, Mapping) or payload.get("code", 0) != 0:
        raise ValueError("tencent_daily_response_rejected")
    data = payload.get("data")
    stock = data.get(symbol) if isinstance(data, Mapping) else None
    if not isinstance(stock, Mapping):
        raise ValueError("tencent_daily_stock_data_missing")
    rows = stock.get("qfqday" if mode == "qfq" else "day")
    if mode == "qfq" and rows is None:
        neutral = stock.get("day")
        if isinstance(neutral, list) and neutral and all(_neutral_adjustment(row) for row in neutral):
            rows = neutral
    if not isinstance(rows, list) or len(rows) > 640:
        raise ValueError("tencent_daily_qualified_rows_missing")
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
        # Decimal preserves tiny nonzero values that float conversion could round to zero.
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number.is_zero()


def _row_date(row: object) -> date:
    if not isinstance(row, list) or not row or not isinstance(row[0], str):
        raise ValueError("tencent_daily_row_invalid")
    return date.fromisoformat(row[0])


def _parse_side(code: str, row: object, mode: str) -> PublishedHistorySide:
    day = _row_date(row)
    if not isinstance(row, list) or len(row) < 9:
        raise ValueError("tencent_daily_row_fields_missing")
    open_price, close, high, low, volume, amount = (float(row[index]) for index in (1, 2, 3, 4, 5, 8))
    if not low <= min(open_price, close) <= max(open_price, close) <= high:
        raise ValueError("tencent_daily_ohlc_range_invalid")
    return PublishedHistorySide(
        code,
        day,
        "qfq" if mode == "qfq" else "unadjusted",
        open_price,
        high,
        low,
        close,
        volume * 100.0,
        amount * 10000.0,
        None,
        None,
        None if mode == "qfq" or row[7] in (None, "") else float(row[7]),
        "trading",
    )
