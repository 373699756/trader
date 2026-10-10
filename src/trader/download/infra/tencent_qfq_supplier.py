"""Tencent supplier for bounded qfq windows."""

from __future__ import annotations

import json
import platform
from collections.abc import Callable
from datetime import date
from typing import Literal

import requests

from trader.download.domain.baostock_daily import (
    BaoStockBoard,
    BaoStockCalendar,
    BaoStockCodeBatch,
    BaoStockCodeDownload,
    BaoStockDailyCell,
    BaoStockDailyFact,
    BaoStockDailySide,
    BaoStockSecurity,
    BaoStockSourceVersions,
)
from trader.download.domain.history_sync import HistorySupplierContext

_ENDPOINTS = {
    "proxy": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "direct": "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
}
_PROXIES = {"http": "", "https": "", "all": ""}
_SHANGHAI = "Asia/Shanghai"


class TencentQfqSupplier:
    """Fetch raw/qfq daily rows concurrently without BaoStock data calls."""

    def __init__(  # noqa: PLR0913 - explicit supplier boundary dependencies
        self,
        context: HistorySupplierContext,
        *,
        workers: int = 8,
        timeout_seconds: float = 15.0,
        retries: int = 2,
        history_host: str = "proxy",
        session_factory: Callable[[], requests.Session] = requests.Session,
        cancel_requested: Callable[[], bool] = lambda: False,
    ) -> None:
        if history_host not in _ENDPOINTS or not 1 <= workers <= 12 or timeout_seconds <= 0 or not 0 <= retries <= 2:
            raise ValueError("Tencent qfq supplier configuration is invalid")
        self._context = context
        self._workers = workers
        self._timeout_seconds = timeout_seconds
        self._retries = retries
        self._history_host = history_host
        self._session_factory = session_factory
        self._cancel_requested = cancel_requested

    def load_qfq_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        del as_of, sessions
        return self._context

    def load_context(self, as_of: date, sessions: int) -> HistorySupplierContext:
        return self.load_qfq_context(as_of, sessions)

    def fetch_code(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        if self._cancel_requested():
            raise RuntimeError("tencent_qfq_cancelled")
        last_error: BaseException | None = None
        for attempt in range(self._retries + 1):
            try:
                return self._fetch_once(security, dates)
            except (OSError, RuntimeError, ValueError, requests.RequestException) as exc:
                last_error = exc
                if attempt < self._retries and not self._cancel_requested():
                    continue
                break
        raise RuntimeError("tencent_qfq_request_failed") from last_error

    def _fetch_once(self, security: BaoStockSecurity, dates: tuple[date, ...]) -> BaoStockCodeDownload:
        raw = self._fetch_side(security.code, dates, False)
        qfq = self._fetch_side(security.code, dates, True)
        raw_by_day = {item.trade_date: item for item in raw}
        qfq_by_day = {item.trade_date: item for item in qfq}
        cells: list[BaoStockDailyCell] = []
        facts: list[BaoStockDailyFact] = []
        for day in dates:
            raw_side = raw_by_day.get(day)
            qfq_side = qfq_by_day.get(day)
            status: Literal["complete", "unknown_missing"] = (
                "complete" if raw_side is not None and qfq_side is not None else "unknown_missing"
            )
            cells.append(BaoStockDailyCell(security.code, day, status, raw_side, qfq_side))
            facts.append(BaoStockDailyFact(security.code, day, False))
        return BaoStockCodeDownload(BaoStockCodeBatch(security.code, tuple(cells)), tuple(facts))

    def _fetch_side(self, code: str, dates: tuple[date, ...], qfq: bool) -> tuple[BaoStockDailySide, ...]:
        if not dates:
            return ()
        symbol = ("sh" if code.startswith("6") else "sz") + code
        end = dates[-1]
        start = dates[0]
        mode = "qfq" if qfq else "bfq"
        with self._session_factory() as session:
            response = session.get(
                _ENDPOINTS[self._history_host],
                params={
                    "_var": f"kline_day{mode}{end.year}",
                    "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},640,{mode}",
                    "r": "0.8205512681390605",
                },
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
                timeout=self._timeout_seconds,
                proxies=_PROXIES,
            )
            response.raise_for_status()
            marker = response.text.find("={")
            if marker < 0:
                raise RuntimeError("tencent_qfq_payload_missing")
            payload = json.loads(response.text[marker + 1 :])
        rows = payload.get("data", {}).get(symbol, {}).get("qfqday" if qfq else "day")
        if not isinstance(rows, list):
            raise RuntimeError("tencent_qfq_rows_missing")
        result: list[BaoStockDailySide] = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 9:
                continue
            try:
                day = date.fromisoformat(str(row[0]))
                values = tuple(float(row[index]) for index in (1, 2, 3, 4, 5, 8))
            except (TypeError, ValueError):
                continue
            if day not in dates:
                continue
            open_price, close, high, low, volume, amount = values
            result.append(
                BaoStockDailySide(
                    code,
                    day,
                    "qfq" if qfq else "unadjusted",
                    open_price,
                    high,
                    low,
                    close,
                    volume * 100.0,
                    amount * 10000.0,
                    None if qfq else close,
                    None if qfq else 0.0,
                    None if qfq else float(row[7]),
                    "trading",
                )
            )
        return tuple(sorted(result, key=lambda item: item.trade_date))


def context_from_manifest(calendar_dates: tuple[date, ...], codes: tuple[str, ...]) -> HistorySupplierContext:
    """Build qfq-only metadata from the already published history identity."""
    if not calendar_dates or not codes:
        raise ValueError("published history manifest is required for Tencent qfq")
    securities = tuple(
        BaoStockSecurity(
            code,
            code,
            _board(code),
            calendar_dates[0],
            None,
            "tencent",
        )
        for code in codes
    )
    return HistorySupplierContext(
        BaoStockCalendar(calendar_dates),
        securities,
        BaoStockSourceVersions("tencent", platform.python_version(), (("endpoint", "tencent"),)),
    )


def _board(code: str) -> BaoStockBoard:
    return "star" if code.startswith("68") else "chinext" if code.startswith("30") else "main"


__all__ = ["TencentQfqSupplier", "context_from_manifest"]
