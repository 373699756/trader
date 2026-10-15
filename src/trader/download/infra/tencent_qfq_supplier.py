"""Bounded Tencent raw/qfq windows with explicit transient failures."""

from __future__ import annotations

import json
import math
import platform
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from queue import Queue
from typing import Literal

import requests

from trader.download.domain.baostock_daily import (
    BaoStockCalendar,
    BaoStockSecurity,
    BaoStockSourceVersions,
    DailyCellStatus,
)
from trader.download.domain.history_supplier_failure import HistorySupplierPendingError, HistorySupplierPendingReason
from trader.download.domain.history_sync import HistorySupplierContext
from trader.download.domain.published_history import PublishedHistoryCell, PublishedHistorySide, PublishedHistoryWindow

_ENDPOINTS = {
    "proxy": "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "direct": "https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get",
}
_PROXIES = {"http": "", "https": "", "all": ""}
_QFQ_PAGE_ROWS = 640
_MAX_QFQ_PAGES = 5


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
    count: int


@dataclass(frozen=True)
class _WindowQuery:
    symbol: str
    dates: tuple[date, ...]
    request_count: int
    allow_interior_padding: bool


@dataclass(frozen=True)
class _SideFetch:
    session: requests.Session
    query: _WindowQuery
    mode: Literal["bfq", "qfq"]
    host: Literal["proxy", "direct"]


@dataclass(frozen=True)
class _SideAttempt:
    sides: tuple[PublishedHistorySide, ...] = ()
    pending: bool = False
    retry_alternate: bool = False
    reason: HistorySupplierPendingReason = "tencent_history_pending"
    invalid_dates: frozenset[date] = frozenset()


@dataclass(frozen=True)
class _SideDownload:
    sides: tuple[PublishedHistorySide, ...]
    pending_dates: tuple[date, ...]
    reason: HistorySupplierPendingReason = "tencent_history_pending"
    invalid_dates: frozenset[date] = frozenset()


class _QfqPagePendingError(Exception):
    def __init__(
        self,
        sides: tuple[PublishedHistorySide, ...],
        reason: HistorySupplierPendingReason,
        invalid_dates: frozenset[date],
        *,
        retry_alternate: bool = False,
    ) -> None:
        self.sides = sides
        self.reason = reason
        self.invalid_dates = invalid_dates
        self.retry_alternate = retry_alternate


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
        return self.load_history_context(as_of, sessions, universe)

    def load_history_context(
        self,
        as_of: date,
        sessions: int,
        universe: tuple[BaoStockSecurity, ...],
    ) -> HistorySupplierContext:
        """Build the shared Tencent calendar and official-universe price context."""

        self._check_cancel()
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
        count = sessions
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
        request_count: int | None = None,
        allow_interior_padding: bool = False,
    ) -> PublishedHistoryWindow:
        count = len(dates) if request_count is None else request_count
        if not 1 <= len(dates) <= 2000 or not 1 <= count <= 2000 or dates != tuple(sorted(set(dates))):
            raise ValueError("Tencent request requires ordered unique dates and a bounded row count")
        symbol = security.source_code.replace(".", "")
        query = _WindowQuery(symbol, dates, count, allow_interior_padding)
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
        request_pending = bool(raw_download.pending_dates or qfq_download.pending_dates)
        invalid_dates = raw_download.invalid_dates | qfq_download.invalid_dates
        window = PublishedHistoryWindow(
            security.code,
            tuple(
                PublishedHistoryCell(
                    security.code,
                    day,
                    "tencent_inferred_suspended"
                    if not request_pending and day not in invalid_dates and day not in raw and day not in qfq
                    else DailyCellStatus(raw.get(day), qfq.get(day)),
                    raw.get(day),
                    qfq.get(day),
                )
                for day in dates
            ),
        )
        if request_pending or invalid_dates:
            reason = (
                qfq_download.reason
                if qfq_download.pending_dates
                else raw_download.reason
                if raw_download.pending_dates
                else "tencent_qfq_invalid_price"
            )
            raise HistorySupplierPendingError(window, reason, tuple(sorted(invalid_dates)))
        return window

    def _download_side(
        self, session: requests.Session, query: _WindowQuery, mode: Literal["bfq", "qfq"]
    ) -> _SideDownload:
        primary = self._options.history_host
        alternate: Literal["proxy", "direct"] = "direct" if primary == "proxy" else "proxy"
        initial = self._attempt_side(session, query, mode, primary)
        if initial.pending and initial.retry_alternate:
            fallback = self._attempt_side(session, query, mode, alternate)
            if fallback.pending:
                best = max((initial, fallback), key=lambda attempt: len(attempt.sides))
                return _SideDownload(best.sides, query.dates, best.reason, best.invalid_dates)
            initial = fallback
        return _SideDownload(
            initial.sides,
            query.dates if initial.pending else (),
            initial.reason,
            initial.invalid_dates,
        )

    def _attempt_side(
        self,
        session: requests.Session,
        query: _WindowQuery,
        mode: Literal["bfq", "qfq"],
        host: Literal["proxy", "direct"],
    ) -> _SideAttempt:
        try:
            sides, invalid_dates = self._fetch_side(session, query, mode, host)
            return _SideAttempt(tuple(sides.values()), invalid_dates=invalid_dates)
        except _QfqPagePendingError as exc:
            return _SideAttempt(
                exc.sides,
                pending=True,
                retry_alternate=exc.retry_alternate,
                reason=exc.reason,
                invalid_dates=exc.invalid_dates,
            )
        except RuntimeError as exc:
            if str(exc) != "tencent_qfq_request_failed":
                raise
            reason: HistorySupplierPendingReason = (
                "tencent_qfq_page_request_failed" if mode == "qfq" else "tencent_history_pending"
            )
            return _SideAttempt(pending=True, retry_alternate=True, reason=reason)
        except ValueError as exc:
            if isinstance(exc, json.JSONDecodeError) or str(exc) == "tencent_daily_response_rejected":
                reason = "tencent_qfq_page_request_failed" if mode == "qfq" else "tencent_history_pending"
                return _SideAttempt(pending=True, retry_alternate=True, reason=reason)
            if str(exc) == "tencent_daily_qualified_rows_missing":
                return _SideAttempt()
            raise

    def _fetch_side(
        self,
        session: requests.Session,
        query: _WindowQuery,
        mode: Literal["bfq", "qfq"],
        host: Literal["proxy", "direct"],
    ) -> tuple[dict[date, PublishedHistorySide], frozenset[date]]:
        dates = query.dates
        result, count, invalid_dates, crossed_start = self._read_side_page(session, query, mode, host, dates[-1])
        if mode != "qfq":
            return result, invalid_dates
        fetch = _SideFetch(session, query, mode, host)
        for _ in range(_MAX_QFQ_PAGES - 1):
            if count < _QFQ_PAGE_ROWS or crossed_start or not result or min(result) <= dates[0]:
                return result, invalid_dates
            boundary = min(result)
            older, count, older_invalid_dates, crossed_start = self._read_older_qfq_page(
                fetch, boundary, result, invalid_dates
            )
            if (
                boundary not in older
                or any(result[day] != side for day, side in older.items() if day in result)
                or invalid_dates.intersection(older)
                or older_invalid_dates.intersection(result)
            ):
                raise _QfqPagePendingError(tuple(result.values()), "tencent_qfq_anchor_conflict", invalid_dates)
            new = {day: side for day, side in older.items() if day < boundary}
            if not new:
                if count < _QFQ_PAGE_ROWS or crossed_start:
                    return result, invalid_dates | older_invalid_dates
                raise _QfqPagePendingError(tuple(result.values()), "tencent_qfq_page_stalled", invalid_dates)
            result.update(new)
            invalid_dates |= older_invalid_dates
        if count == _QFQ_PAGE_ROWS and not crossed_start and min(result) > dates[0]:
            raise _QfqPagePendingError(tuple(result.values()), "tencent_qfq_page_limit", invalid_dates)
        return result, invalid_dates

    def _read_older_qfq_page(
        self,
        fetch: _SideFetch,
        boundary: date,
        current: dict[date, PublishedHistorySide],
        invalid_dates: frozenset[date],
    ) -> tuple[dict[date, PublishedHistorySide], int, frozenset[date], bool]:
        try:
            return self._read_side_page(fetch.session, fetch.query, fetch.mode, fetch.host, boundary)
        except RuntimeError as exc:
            if str(exc) == "tencent_qfq_cancelled":
                raise
            raise _QfqPagePendingError(
                tuple(current.values()),
                "tencent_qfq_page_request_failed",
                invalid_dates,
                retry_alternate=True,
            ) from exc
        except ValueError as exc:
            retry_alternate = isinstance(exc, json.JSONDecodeError) or str(exc) == "tencent_daily_response_rejected"
            reason: HistorySupplierPendingReason = (
                "tencent_qfq_page_request_failed" if retry_alternate else "tencent_qfq_page_invalid"
            )
            raise _QfqPagePendingError(
                tuple(current.values()),
                reason,
                invalid_dates,
                retry_alternate=retry_alternate,
            ) from exc

    def _read_side_page(
        self,
        session: requests.Session,
        query: _WindowQuery,
        mode: Literal["bfq", "qfq"],
        host: Literal["proxy", "direct"],
        end: date,
    ) -> tuple[dict[date, PublishedHistorySide], int, frozenset[date], bool]:
        symbol, dates = query.symbol, query.dates
        rows = self._request_rows(
            _DailyQuery(symbol, dates[0], end, mode, query.request_count), session=session, host=host
        )
        result: dict[date, PublishedHistorySide] = {}
        invalid_dates: set[date] = set()
        crossed_start = False
        expected = frozenset(dates)
        previous: date | None = None
        for row in rows:
            day = _row_date(row)
            if (previous is not None and day <= previous) or day > end:
                raise ValueError("tencent_daily_rows_outside_calendar_or_duplicated")
            previous = day
            # Tencent may return older padding despite the requested start date.
            # Only that documented direction may be trimmed; future/interior anomalies fail closed.
            if day < dates[0]:
                crossed_start = True
                continue
            if day not in expected:
                if not query.allow_interior_padding:
                    raise ValueError("tencent_daily_rows_outside_calendar_or_duplicated")
                if mode != "qfq":
                    continue
            if mode == "qfq" and _unusable_qfq_price(row):
                if day in expected:
                    invalid_dates.add(day)
                continue
            side = _parse_side(symbol[2:], row, mode)
            result[side.trade_date] = side
        return result, len(rows), frozenset(invalid_dates), crossed_start

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


def _payload_rows(payload: object, symbol: str, mode: str, count: int) -> list[object]:
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
        elif neutral is not None:
            raise ValueError("tencent_daily_qualified_rows_missing")
    # Tencent can append the current session to count historical rows.
    # Calendar/window consumers still validate dates and trim only older padding.
    if rows is None:
        raise ValueError("tencent_daily_response_shape_invalid")
    if not isinstance(rows, list) or len(rows) > count + 1:
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
        # Decimal preserves tiny nonzero values that float conversion could round to zero.
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number.is_zero()


def _row_date(row: object) -> date:
    if not isinstance(row, list) or not row or not isinstance(row[0], str):
        raise ValueError("tencent_daily_row_invalid")
    return date.fromisoformat(row[0])


def _unusable_qfq_price(row: object) -> bool:
    if not isinstance(row, list) or len(row) < 9:
        raise ValueError("tencent_daily_row_fields_missing")
    prices = tuple(float(row[index]) for index in (1, 2, 3, 4))
    open_price, close, high, low = prices
    if not all(math.isfinite(value) for value in prices) or not (
        low <= min(open_price, close) <= max(open_price, close) <= high
    ):
        raise ValueError("tencent_daily_ohlc_range_invalid")
    return any(value <= 0 for value in prices)


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
