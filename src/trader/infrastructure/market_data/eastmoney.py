"""Eastmoney full-market quote, daily-history and targeted minute adapters."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import as_completed
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast
from zoneinfo import ZoneInfo

import requests

from trader.application.workers import BoundedExecutor, borrow_executor
from trader.domain.models import MarketQuote
from trader.domain.tail import MinuteBar
from trader.infrastructure.market_data.history import DailyBar


class JsonResponse(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


SessionFactory = Callable[[], requests.Session]

FIELDS = "f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13,f14,f15,f16,f17,f18,f20,f21,f22,f23,f24,f25,f100,f124"
HOSTS = ("82.push2.eastmoney.com", "push2.eastmoney.com", "7.push2.eastmoney.com")
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_REQUEST_ROUNDS = 2
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class EastmoneyClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        workers: int = 6,
        page_size: int = 500,
        worker_pool: BoundedExecutor | None = None,
        session_factory: SessionFactory = requests.Session,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._workers = max(1, workers)
        self._page_size = max(100, page_size)
        self._worker_pool = worker_pool
        self._session_factory = session_factory

    def fetch_market(self, now: datetime | None = None) -> tuple[MarketQuote, ...]:
        received_at = now or datetime.now(timezone.utc)
        first = self._fetch_page(1)
        data = _object_mapping(first.get("data"))
        first_rows = _object_rows(data.get("diff"))
        if not first_rows:
            raise RuntimeError("eastmoney returned an empty first page")
        total = int(_to_float(data.get("total")) or len(first_rows))
        page_count = max(1, math.ceil(total / self._page_size))
        pages: dict[int, list[Mapping[str, object]]] = {1: first_rows}
        if page_count > 1:
            remaining: list[tuple[int, Mapping[str, object]]] = []
            worker_pool = self._worker_pool
            if worker_pool is not None and worker_pool.owns_current_thread() and worker_pool.worker_count == 1:
                remaining.extend((page, self._fetch_page(page)) for page in range(2, page_count + 1))
            else:
                with borrow_executor(
                    worker_pool,
                    worker_count=min(self._workers, page_count - 1),
                    thread_name_prefix="eastmoney",
                    queue_capacity=page_count - 1,
                ) as pool:
                    futures = {}
                    for page in range(2, page_count + 1):
                        future = pool.submit(self._fetch_page, page)
                        if future is None:
                            raise RuntimeError("data worker queue rejected Eastmoney page task")
                        futures[future] = page
                    remaining.extend((futures[future], future.result()) for future in as_completed(futures))
            for page, payload in remaining:
                rows = _object_rows(_object_mapping(payload.get("data")).get("diff"))
                if not rows:
                    raise RuntimeError(f"eastmoney page {page} was empty")
                pages[page] = rows
        raw_rows = [row for page in sorted(pages) for row in pages[page]]
        quotes = tuple(quote for row in raw_rows if (quote := _quote_from_row(row, received_at)) is not None)
        if len({quote.code for quote in quotes}) < min(1000, total // 2):
            raise RuntimeError(f"eastmoney quote coverage is incomplete: {len(quotes)}/{total}")
        return quotes

    def fetch_history(self, code: str, *, days: int = 90, now: datetime | None = None) -> tuple[DailyBar, ...]:
        end = (now or datetime.now(timezone.utc)).date()
        start = end - timedelta(days=max(days * 2, 180))
        payload = self._get(
            ("push2his.eastmoney.com", "82.push2his.eastmoney.com", "7.push2his.eastmoney.com"),
            "/api/qt/stock/kline/get",
            {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": "1",
                "secid": _secid(code),
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            },
        )
        klines = _object_mapping(payload.get("data")).get("klines")
        if not isinstance(klines, list):
            return ()
        bars: list[DailyBar] = []
        for raw in klines:
            parts = str(raw).split(",")
            if len(parts) < 9:
                continue
            values = [_to_float(value) for value in parts[1:9]]
            if any(value is None for value in values):
                continue
            open_price, close, high, low, volume, amount, _amplitude, pct_change = cast(list[float], values)
            bars.append(DailyBar(parts[0], open_price, close, high, low, volume, amount, pct_change))
        return tuple(bars[-days:])

    def fetch_intraday_minutes(self, code: str, *, now: datetime | None = None) -> tuple[MinuteBar, ...]:
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("intraday observation time must be timezone-aware")
        observed_local = observed_at.astimezone(_SHANGHAI)
        payload = self._get(
            ("push2his.eastmoney.com",),
            "/api/qt/stock/trends2/get",
            {
                "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "ndays": "1",
                "iscr": "0",
                "secid": _secid(code),
            },
            request_rounds=1,
        )
        trends = _object_mapping(payload.get("data")).get("trends")
        if not isinstance(trends, list):
            return ()
        bars: list[MinuteBar] = []
        for raw in trends:
            parts = str(raw).split(",")
            if len(parts) < 8:
                continue
            try:
                source_time = datetime.strptime(parts[0], "%Y-%m-%d %H:%M").replace(tzinfo=_SHANGHAI)
            except ValueError:
                continue
            close = _to_float(parts[2])
            volume = _to_float(parts[5])
            if (
                source_time.date() != observed_local.date()
                or source_time > observed_local
                or close is None
                or close <= 0.0
            ):
                continue
            bars.append(
                MinuteBar(
                    source_time=source_time,
                    close=close,
                    volume=volume if volume is not None and volume >= 0.0 else None,
                    source="eastmoney_intraday",
                    received_time=observed_at,
                    data_version=f"eastmoney-intraday:{int(observed_at.timestamp())}",
                )
            )
        return tuple(bars)

    def _fetch_page(self, page: int) -> Mapping[str, object]:
        return self._get(
            HOSTS,
            "/api/qt/clist/get",
            {
                "pn": str(page),
                "pz": str(self._page_size),
                "po": "1",
                "np": "1",
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": "2",
                "invt": "2",
                "fid": "f12",
                "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23,m:0 t:81 s:2048",
                "fields": FIELDS,
            },
        )

    def _get(
        self,
        hosts: Sequence[str],
        path: str,
        params: Mapping[str, str],
        *,
        request_rounds: int = _REQUEST_ROUNDS,
    ) -> Mapping[str, object]:
        last_error: Exception | None = None
        headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}
        for _round in range(max(1, request_rounds)):
            for host in hosts:
                try:
                    with self._session_factory() as session:
                        response = session.get(
                            f"https://{host}{path}",
                            params=dict(params),
                            headers=headers,
                            timeout=self._timeout_seconds,
                            proxies=_DIRECT_PROXIES,
                        )
                        response.raise_for_status()
                        payload = response.json()
                except (requests.RequestException, ValueError, OSError) as exc:
                    last_error = exc
                    continue
                if isinstance(payload, dict) and payload.get("data"):
                    return payload
                last_error = RuntimeError("eastmoney returned empty data")
        raise RuntimeError(f"eastmoney request failed: {last_error}") from last_error


def _quote_from_row(row: Mapping[str, object], received_at: datetime) -> MarketQuote | None:
    code = str(row.get("f12") or "").strip()
    if len(code) != 6 or not code.isdigit():
        return None
    name = str(row.get("f14") or "").strip()
    source_time = _source_time(row.get("f124"), received_at)
    price = _to_float(row.get("f2"))
    high = _to_float(row.get("f15"))
    low = _to_float(row.get("f16"))
    pct_change = _to_float(row.get("f3"))
    is_one_price_limit = bool(
        price
        and high
        and low
        and abs(high - low) < 1e-9
        and pct_change is not None
        and abs(pct_change) >= (19.5 if code.startswith(("300", "301", "688", "689")) else 9.5)
    )
    return MarketQuote(
        code=code,
        name=name,
        price=price,
        previous_close=_to_float(row.get("f18")),
        open_price=_to_float(row.get("f17")),
        high=high,
        low=low,
        pct_change=pct_change,
        change_5m=_to_float(row.get("f11")),
        speed=_to_float(row.get("f22")),
        volume_ratio=_to_float(row.get("f10")),
        turnover_rate=_to_float(row.get("f8")),
        amount=_to_float(row.get("f6")),
        amplitude=_to_float(row.get("f7")),
        market_cap=_to_float(row.get("f20")),
        industry=str(row.get("f100") or "").strip(),
        source="eastmoney",
        source_time=source_time,
        received_time=received_at,
        data_version=f"eastmoney:{int(received_at.timestamp())}",
        is_st="ST" in name.upper() or "退" in name,
        is_suspended=price is None or price <= 0,
        is_one_price_limit=is_one_price_limit,
    )


def _source_time(raw: object, fallback: datetime) -> datetime:
    value = _to_float(raw)
    if value is None or value <= 0:
        return fallback
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return fallback


def _secid(code: str) -> str:
    return f"1.{code}" if code.startswith(("5", "6", "9")) else f"0.{code}"


def _to_float(value: object) -> float | None:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _object_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _object_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


__all__ = ["EastmoneyClient"]
