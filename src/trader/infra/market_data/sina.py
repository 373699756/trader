"""Sina full-market quote fallback adapter."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from concurrent.futures import as_completed
from datetime import datetime, timezone

import requests

from trader.application.workers import borrow_executor, submit_or_run_inline
from trader.domain.market.models import MarketQuote
from trader.infra.market_data.normalize import MarketQuoteInput, build_market_quote, normalize_quotes, to_float

SessionFactory = Callable[[], requests.Session]
COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
QUOTE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
_DIRECT_PROXIES = {"http": "", "https": "", "all": ""}
_REQUEST_ATTEMPTS = 2


class SinaClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        workers: int = 5,
        page_size: int = 80,
        session_factory: SessionFactory = requests.Session,
        cancel_requested: Callable[[], bool] = lambda: False,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._workers = max(1, workers)
        self._page_size = max(20, min(100, page_size))
        self._session_factory = session_factory
        self._cancel_requested = cancel_requested
        self._wall_clock = wall_clock

    def fetch_market(self, now: datetime | None = None) -> tuple[MarketQuote, ...]:
        total_text = self._get_text(COUNT_URL, {"node": "hs_a"})
        total_match = re.search(r"\d+", total_text)
        if total_match is None:
            raise RuntimeError("sina quote count was invalid")
        total = int(total_match.group(0))
        page_count = max(1, math.ceil(total / self._page_size))
        pages: dict[int, list[Mapping[str, object]]] = {}
        with borrow_executor(
            None,
            worker_count=min(self._workers, page_count),
            queue_capacity=page_count,
            thread_name_prefix="sina-market",
        ) as pool:
            futures = {submit_or_run_inline(pool, self._fetch_page, page): page for page in range(1, page_count + 1)}
            for future in as_completed(futures):
                page = futures[future]
                payload = future.result()
                if not isinstance(payload, list):
                    raise RuntimeError(f"sina page {page} was not a list")
                pages[page] = [item for item in payload if isinstance(item, dict)]
        rows: list[Mapping[str, object]] = []
        for page in range(1, page_count + 1):
            payload = pages[page]
            if not isinstance(payload, list):
                raise RuntimeError(f"sina page {page} was not a list")
            rows.extend(payload)
        received_at = now or self._wall_clock()
        quotes = normalize_quotes(rows, received_at, normalizer=_quote_from_row)
        if len({quote.code for quote in quotes}) < min(1000, total // 2):
            raise RuntimeError(f"sina quote coverage is incomplete: {len(quotes)}/{total}")
        return quotes

    def _fetch_page(self, page: int) -> object:
        return self._get_json(
            QUOTE_URL,
            {
                "page": str(page),
                "num": str(self._page_size),
                "sort": "symbol",
                "asc": "1",
                "node": "hs_a",
                "symbol": "",
                "_s_r_a": "page",
            },
        )

    def _get_text(self, url: str, params: Mapping[str, str]) -> str:
        value = self._request(url, params, as_json=False)
        if not isinstance(value, str):
            raise RuntimeError("sina text response was invalid")
        return value

    def _get_json(self, url: str, params: Mapping[str, str]) -> object:
        return self._request(url, params, as_json=True)

    def _request(self, url: str, params: Mapping[str, str], *, as_json: bool) -> object:
        last_error: Exception | None = None
        for _attempt in range(_REQUEST_ATTEMPTS):
            self._ensure_running()
            try:
                with self._session_factory() as session:
                    response = session.get(
                        url,
                        params=dict(params),
                        headers={"User-Agent": "Mozilla/5.0"},
                        timeout=self._timeout_seconds,
                        proxies=_DIRECT_PROXIES,
                    )
                    response.raise_for_status()
                    result = response.json() if as_json else response.text
            except (requests.RequestException, ValueError, OSError) as exc:
                last_error = exc
                continue
            self._ensure_running()
            return result
        raise RuntimeError(f"sina request failed after {_REQUEST_ATTEMPTS} attempts: {last_error}") from last_error

    def _ensure_running(self) -> None:
        if self._cancel_requested():
            raise RuntimeError("sina source lane stopped")


def _quote_from_row(row: Mapping[str, object], received_at: datetime) -> MarketQuote | None:
    raw_code = str(row.get("code") or row.get("symbol") or "")
    code_match = re.search(r"(\d{6})", raw_code)
    if code_match is None:
        return None
    code = code_match.group(1)
    name = str(row.get("name") or "").strip()
    high = to_float(row.get("high"))
    low = to_float(row.get("low"))
    pct_change = to_float(row.get("changepercent"))
    price = to_float(row.get("trade"))
    settlement = to_float(row.get("settlement"))
    return build_market_quote(
        MarketQuoteInput(
            code=code,
            name=name,
            price=price,
            previous_close=settlement,
            open_price=to_float(row.get("open")),
            high=high,
            low=low,
            pct_change=pct_change,
            change_5m=None,
            speed=None,
            volume_ratio=None,
            turnover_rate=to_float(row.get("turnoverratio")),
            amount=to_float(row.get("amount")),
            amplitude=_amplitude(high, low, settlement),
            market_cap=_scaled_market_cap(row.get("mktcap")),
            industry="",
            source="sina",
            source_time=received_at,
            received_time=received_at,
            data_version=f"sina:{int(received_at.timestamp())}",
            is_st="ST" in name.upper() or "退" in name,
            is_suspended=price is None or price <= 0,
        )
    )


def _amplitude(high: float | None, low: float | None, previous_close: float | None) -> float | None:
    if high is None or low is None or previous_close is None or previous_close <= 0:
        return None
    return (high - low) / previous_close * 100.0


def _scaled_market_cap(raw: object) -> float | None:
    value = to_float(raw)
    return value * 10_000 if value is not None else None


__all__ = ["SinaClient"]
