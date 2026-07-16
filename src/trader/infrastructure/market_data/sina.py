"""Sina full-market quote fallback adapter."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timezone

import requests

from trader.domain.models import MarketQuote

SessionFactory = Callable[[], requests.Session]
COUNT_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
QUOTE_URL = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"


class SinaClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        page_size: int = 80,
        session_factory: SessionFactory = requests.Session,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._page_size = max(20, min(100, page_size))
        self._session_factory = session_factory

    def fetch_market(self, now: datetime | None = None) -> tuple[MarketQuote, ...]:
        received_at = now or datetime.now(timezone.utc)
        total_text = self._get_text(COUNT_URL, {"node": "hs_a"})
        total_match = re.search(r"\d+", total_text)
        if total_match is None:
            raise RuntimeError("sina quote count was invalid")
        total = int(total_match.group(0))
        rows: list[Mapping[str, object]] = []
        for page in range(1, max(1, math.ceil(total / self._page_size)) + 1):
            payload = self._get_json(
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
            if not isinstance(payload, list):
                raise RuntimeError(f"sina page {page} was not a list")
            rows.extend(item for item in payload if isinstance(item, dict))
        quotes = tuple(quote for row in rows if (quote := _quote_from_row(row, received_at)) is not None)
        if len({quote.code for quote in quotes}) < min(1000, total // 2):
            raise RuntimeError(f"sina quote coverage is incomplete: {len(quotes)}/{total}")
        return quotes

    def _get_text(self, url: str, params: Mapping[str, str]) -> str:
        with self._session_factory() as session:
            response = session.get(
                url, params=dict(params), headers={"User-Agent": "Mozilla/5.0"}, timeout=self._timeout_seconds
            )
            response.raise_for_status()
            return response.text

    def _get_json(self, url: str, params: Mapping[str, str]) -> object:
        with self._session_factory() as session:
            response = session.get(
                url, params=dict(params), headers={"User-Agent": "Mozilla/5.0"}, timeout=self._timeout_seconds
            )
            response.raise_for_status()
            return response.json()


def _quote_from_row(row: Mapping[str, object], received_at: datetime) -> MarketQuote | None:
    raw_code = str(row.get("code") or row.get("symbol") or "")
    code_match = re.search(r"(\d{6})", raw_code)
    if code_match is None:
        return None
    code = code_match.group(1)
    name = str(row.get("name") or "").strip()
    price = _number(row.get("trade"))
    high = _number(row.get("high"))
    low = _number(row.get("low"))
    pct_change = _number(row.get("changepercent"))
    return MarketQuote(
        code=code,
        name=name,
        price=price,
        previous_close=_number(row.get("settlement")),
        open_price=_number(row.get("open")),
        high=high,
        low=low,
        pct_change=pct_change,
        change_5m=None,
        speed=None,
        volume_ratio=None,
        turnover_rate=_number(row.get("turnoverratio")),
        amount=_number(row.get("amount")),
        amplitude=_amplitude(high, low, _number(row.get("settlement"))),
        market_cap=_scaled_market_cap(row.get("mktcap")),
        industry="",
        source="sina",
        source_time=received_at,
        received_time=received_at,
        data_version=f"sina:{int(received_at.timestamp())}",
        is_st="ST" in name.upper() or "退" in name,
        is_suspended=price is None or price <= 0,
    )


def _amplitude(high: float | None, low: float | None, previous_close: float | None) -> float | None:
    if high is None or low is None or previous_close is None or previous_close <= 0:
        return None
    return (high - low) / previous_close * 100.0


def _scaled_market_cap(raw: object) -> float | None:
    value = _number(raw)
    return value * 10_000 if value is not None else None


def _number(raw: object) -> float | None:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


__all__ = ["SinaClient"]
