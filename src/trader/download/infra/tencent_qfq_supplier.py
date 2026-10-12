"""Bounded Tencent raw/qfq windows with qualified paging and explicit transient failures."""

from __future__ import annotations

import json
import math
import platform
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from queue import Queue
from typing import Literal

import requests

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockSecurity,
    BaoStockSourceVersions,
    daily_cell_status,
)
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError
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
    session_factory: Callable[[], AbstractContextManager[requests.Session]]
    load_universe: Callable[[date], tuple[BaoStockSecurity, ...]]
    cancel_requested: Callable[[], bool]


@dataclass(frozen=True)
class _DailyQuery:
    symbol: str
    start: date
    end: date
    mode: Literal["bfq", "qfq"]
    count: int = 640


@dataclass(frozen=True)
class _WindowQuery:
    symbol: str
    dates: tuple[date, ...]
    request_count: int
    allow_interior_padding: bool
    overlap_dates: tuple[date, ...] = ()
    primary_queried: bool = False


@dataclass(frozen=True)
class _SideAttempt:
    sides: tuple[PublishedHistorySide, ...] = ()
    pending: bool = False
    conflicted: bool = False


@dataclass(frozen=True)
class _SideDownload:
    sides: tuple[PublishedHistorySide, ...]
    pending_dates: tuple[date, ...]


class TencentSessionPool:
    """Borrow bootstrap-owned sessions exclusively so keep-alive survives stock boundaries."""

    def __init__(self, sessions: tuple[requests.Session, ...]) -> None:
        if not sessions:
            raise ValueError("Tencent qfq session pool cannot be empty")
        self._available: Queue[requests.Session] = Queue(maxsize=len(sessions))
        for session in sessions:
            self._available.put_nowait(session)

    @contextmanager
    def borrow(self) -> Iterator[requests.Session]:
        session = self._available.get()
        try:
            yield session
        finally:
            self._available.put_nowait(session)


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
        calendar = self.load_calendar(as_of, sessions)
        return HistorySupplierContext(
            calendar,
            tuple(sorted(universe, key=lambda security: security.code)),
            BaoStockSourceVersions(
                "tencent-newfqkline", platform.python_version(), (("host", self._options.history_host),)
            ),
        )

    def load_calendar(self, as_of: date, sessions: int) -> BaoStockCalendar:
        """Read exchange index dates; never infer sessions from weekday arithmetic."""
        if not 1 <= sessions <= 2000:
            raise ValueError("Tencent calendar sessions must be within 1..2000")
        count = max(640, sessions)
        query = _DailyQuery("sh000001", as_of - timedelta(days=count * 2), as_of, "bfq", count)
        try:
            rows = self._request_rows(query)
        except RuntimeError:
            alternate: Literal["proxy", "direct"] = "direct" if self._options.history_host == "proxy" else "proxy"
            self._check_cancel()
            rows = self._request_rows(query, host=alternate)
        dates = tuple(_row_date(row) for row in rows)
        if dates != tuple(sorted(set(dates))) or any(day > as_of for day in dates) or len(dates) < sessions:
            raise ValueError("Tencent exchange calendar is incomplete or invalid")
        return BaoStockCalendar(dates[-sessions:])

    def fetch_window(
        self,
        security: BaoStockSecurity,
        dates: tuple[date, ...],
        *,
        request_count: int = 640,
        allow_interior_padding: bool = False,
    ) -> PublishedHistoryWindow:
        if not 1 <= len(dates) <= 2000 or not 1 <= request_count <= 2000 or dates != tuple(sorted(set(dates))):
            raise ValueError("Tencent request requires ordered unique dates and a bounded row count")
        symbol = security.source_code.replace(".", "")
        query = _WindowQuery(symbol, dates, request_count, allow_interior_padding)
        with self._dependencies.session_factory() as session:
            raw_download = self._download_side(session, query, "bfq")
            if raw_download.pending_dates == dates and not raw_download.sides:
                raise HistorySupplierPendingError(
                    PublishedHistoryWindow(
                        security.code,
                        tuple(PublishedHistoryCell(security.code, day, "unknown_missing", None, None) for day in dates),
                    )
                )
            qfq_download = self._download_side(session, query, "qfq")
        self._check_cancel()
        raw = {side.trade_date: side for side in raw_download.sides}
        qfq = {side.trade_date: side for side in qfq_download.sides}
        window = PublishedHistoryWindow(
            security.code,
            tuple(
                PublishedHistoryCell(
                    security.code, day, daily_cell_status(raw.get(day), qfq.get(day)), raw.get(day), qfq.get(day)
                )
                for day in dates
            ),
        )
        if raw_download.pending_dates or qfq_download.pending_dates:
            raise HistorySupplierPendingError(window)
        return window

    def _download_side(
        self, session: requests.Session, query: _WindowQuery, mode: Literal["bfq", "qfq"]
    ) -> _SideDownload:
        primary = self._options.history_host
        alternate: Literal["proxy", "direct"] = "direct" if primary == "proxy" else "proxy"
        initial = self._attempt_side(session, query, mode, primary)
        if initial.pending:
            initial = self._attempt_side(session, query, mode, alternate)
            if initial.pending:
                return _SideDownload((), query.dates)
        result = {side.trade_date: side for side in initial.sides}
        if len(result) == len(query.dates):
            return _SideDownload(tuple(result.values()), ())
        pending: set[date] = set()
        # Walk backwards so older pages overlap the already qualified newer page.
        remaining = list(query.dates)
        while remaining:
            end = remaining[-1]
            start = max(0, len(remaining) - 500)
            while remaining[start] < end - timedelta(days=700):
                start += 1
            core = tuple(remaining[start:])
            del remaining[start:]
            missing = tuple(day for day in core if day not in result)
            if not missing:
                continue
            anchors = tuple(sorted(day for day in result if end < day <= end + timedelta(days=30)))[:5]
            page_dates = tuple(sorted(set(core) | set(anchors)))
            page = replace(
                query,
                dates=page_dates,
                request_count=640,
                overlap_dates=anchors,
                primary_queried=page_dates == query.dates,
            )
            recovered = self._recover_page(session, page, mode, tuple(result.values()))
            result = {side.trade_date: side for side in recovered.sides}
            if recovered.pending:
                pending.update(day for day in core if day not in result)
            if recovered.conflicted and any(day not in result for day in core):
                # Do not attach older pages across a rejected adjustment boundary.
                if recovered.pending:
                    pending.update(remaining)
                break
        return _SideDownload(tuple(result[day] for day in sorted(result)), tuple(sorted(pending)))

    def _recover_page(
        self,
        session: requests.Session,
        query: _WindowQuery,
        mode: Literal["bfq", "qfq"],
        previous: tuple[PublishedHistorySide, ...],
    ) -> _SideAttempt:
        primary = self._options.history_host
        alternate: Literal["proxy", "direct"] = "direct" if primary == "proxy" else "proxy"
        # Small windows have already been requested on the primary host.
        hosts = (alternate,) if query.primary_queried else (primary, alternate)
        result = {side.trade_date: side for side in previous}
        pending = conflicted = False
        for host in hosts:
            attempt = self._attempt_side(session, query, mode, host)
            pending = attempt.pending
            incoming = {side.trade_date: side for side in attempt.sides}
            overlap = result.keys() & incoming.keys()
            if any(not _same_prices(result[day], incoming[day]) for day in overlap):
                conflicted = True
                continue
            if not set(query.overlap_dates).issubset(incoming):
                continue
            for day, side in incoming.items():
                result.setdefault(day, side)
            if all(day in result for day in query.dates):
                break
        return _SideAttempt(tuple(result.values()), pending, conflicted)

    def _attempt_side(
        self,
        session: requests.Session,
        query: _WindowQuery,
        mode: Literal["bfq", "qfq"],
        host: Literal["proxy", "direct"],
    ) -> _SideAttempt:
        try:
            return _SideAttempt(tuple(self._fetch_side(session, query, mode, host).values()))
        except RuntimeError as exc:
            if str(exc) != "tencent_qfq_request_failed":
                raise
            return _SideAttempt(pending=True)
        except ValueError as exc:
            # Rejected/undecodable responses can be throttling, not missing capability.
            return _SideAttempt(
                pending=isinstance(exc, json.JSONDecodeError) or str(exc) == "tencent_daily_response_rejected"
            )

    def _fetch_side(
        self,
        session: requests.Session,
        query: _WindowQuery,
        mode: Literal["bfq", "qfq"],
        host: Literal["proxy", "direct"],
    ) -> dict[date, PublishedHistorySide]:
        symbol, dates = query.symbol, query.dates
        rows = self._request_rows(
            _DailyQuery(symbol, dates[0], dates[-1], mode, query.request_count), session=session, host=host
        )
        result: dict[date, PublishedHistorySide] = {}
        expected = frozenset(dates)
        seen: set[date] = set()
        for row in rows:
            day = _row_date(row)
            if day in seen or day > dates[-1]:
                raise ValueError("tencent_daily_rows_outside_calendar_or_duplicated")
            seen.add(day)
            # Tencent may return older padding despite the requested start date.
            # Only that documented direction may be trimmed; future/interior anomalies fail closed.
            if day < dates[0]:
                continue
            if day not in expected:
                if query.allow_interior_padding:
                    continue
                raise ValueError("tencent_daily_rows_outside_calendar_or_duplicated")
            side = _parse_side(symbol[2:], row, mode)
            result[side.trade_date] = side
        return result

    def _request_rows(
        self,
        query: _DailyQuery,
        *,
        session: requests.Session | None = None,
        host: Literal["proxy", "direct"] | None = None,
    ) -> list[object]:
        for attempt in range(self._options.retries + 1):
            self._check_cancel()
            try:
                if session is not None:
                    return self._request_once(session, query, host or self._options.history_host)
                with self._dependencies.session_factory() as owned_session:
                    return self._request_once(owned_session, query, host or self._options.history_host)
            except (requests.RequestException, OSError) as exc:
                if attempt == self._options.retries:
                    raise RuntimeError("tencent_qfq_request_failed") from exc
        raise RuntimeError("tencent_qfq_request_failed")

    def _request_once(
        self, session: requests.Session, query: _DailyQuery, host: Literal["proxy", "direct"]
    ) -> list[object]:
        symbol, start, end, mode, count = query.symbol, query.start, query.end, query.mode, query.count
        response = session.get(
            _ENDPOINTS[host],
            params={
                "_var": f"kline_day{mode}{end.year}",
                "param": f"{symbol},day,{start.isoformat()},{end.isoformat()},{count},{mode}",
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
        return _payload_rows(payload, symbol, mode, count)

    def _check_cancel(self) -> None:
        if self._dependencies.cancel_requested():
            raise RuntimeError("tencent_qfq_cancelled")


def _payload_rows(payload: object, symbol: str, mode: str, count: int = 640) -> list[object]:
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
    if not isinstance(rows, list) or len(rows) > count:
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


def _same_prices(left: PublishedHistorySide, right: PublishedHistorySide) -> bool:
    return all(
        a is not None and b is not None and math.isclose(a, b, rel_tol=0.0, abs_tol=tolerance)
        for a, b, tolerance in (
            (left.open_price, right.open_price, 0.0051),
            (left.close_price, right.close_price, 0.0051),
            (left.high_price, right.high_price, 0.0051),
            (left.low_price, right.low_price, 0.0051),
            (left.volume, right.volume, 100.0),
            (left.amount, right.amount, 100.0),
        )
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
