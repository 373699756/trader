"""Diagnostic-only Eastmoney spot probe; this endpoint has no production lane."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from concurrent.futures import as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from trader.infra.workers import BoundedExecutor, SubmitOrReject
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.infra.normalization.quote import BuildMarketQuote, MarketQuoteInput, ToFloat

_FIELDS = "f2,f3,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f100"
_PAGE_SIZE = 100


def ParseQuote(row: Mapping[str, object], received_at: datetime) -> MarketQuote | None:
    code = str(row.get("f12") or "")
    if len(code) != 6 or not code.isdigit():
        return None
    return BuildMarketQuote(
        MarketQuoteInput(
            code=code,
            name=str(row.get("f14") or ""),
            price=ToFloat(row.get("f2")),
            previous_close=ToFloat(row.get("f18")),
            open_price=ToFloat(row.get("f17")),
            high=ToFloat(row.get("f15")),
            low=ToFloat(row.get("f16")),
            pct_change=ToFloat(row.get("f3")),
            change_5m=ToFloat(row.get("f11")),
            speed=ToFloat(row.get("f22")),
            volume_ratio=ToFloat(row.get("f10")),
            turnover_rate=ToFloat(row.get("f8")),
            amount=ToFloat(row.get("f6")),
            amplitude=ToFloat(row.get("f7")),
            market_cap=ToFloat(row.get("f20")),
            industry=str(row.get("f100") or ""),
            source="eastmoney",
            source_time=received_at,
            received_time=received_at,
            data_version=f"eastmoney:{received_at.isoformat()}",
        )
    )


def _page(number: int, timeout: float, deadline: float) -> tuple[tuple[MarketQuote, ...], int]:
    last_error: Exception | None = None
    for host in ("82.push2.eastmoney.com", "push2.eastmoney.com", "7.push2.eastmoney.com"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("eastmoney diagnostic deadline")
        try:
            with requests.Session() as session:
                response = session.get(
                    f"https://{host}/api/qt/clist/get",
                    params={
                        "pn": str(number),
                        "pz": str(_PAGE_SIZE),
                        "po": "1",
                        "np": "1",
                        "fltt": "2",
                        "invt": "2",
                        "fid": "f12",
                        "fs": "m:0 t:6,m:0 t:80,m:1 t:2,m:1 t:23",
                        "fields": _FIELDS,
                        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                    },
                    timeout=min(timeout, remaining),
                    headers={"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"},
                    proxies={"http": "", "https": "", "all": ""},
                )
                response.raise_for_status()
                payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
                raise ValueError("eastmoney malformed page")
            at = datetime.now(ZoneInfo("Asia/Shanghai"))
            quotes = tuple(
                quote for row in data["diff"] if isinstance(row, dict) and (quote := ParseQuote(row, at)) is not None
            )
            return quotes, int(data.get("total", 0))
        except (requests.RequestException, OSError, ValueError) as exc:
            last_error = exc
    raise RuntimeError("eastmoney diagnostic request failed") from last_error


def FetchEastmoneyQuotes(timeout: float, deadline: float, pool: BoundedExecutor) -> tuple[MarketQuote, ...]:
    first, total = _page(1, timeout, deadline)
    pages = math.ceil(total / _PAGE_SIZE)
    if not first or not 1 <= pages <= 64:
        raise ValueError("eastmoney invalid population size")
    quotes = list(first)
    for start in range(2, pages + 1, 6):
        futures = [
            SubmitOrReject(pool, _page, page, timeout, deadline) for page in range(start, min(start + 6, pages + 1))
        ]
        try:
            for future in as_completed(futures, timeout=max(0.001, deadline - time.monotonic())):
                rows, page_total = future.result()
                if page_total != total:
                    raise ValueError("eastmoney population changed")
                quotes.extend(rows)
        finally:
            for future in futures:
                future.cancel()
    if len({quote.code for quote in quotes}) < total * 0.99:
        raise ValueError("eastmoney incomplete population")
    return tuple(quotes)


__all__ = ["ParseQuote", "FetchEastmoneyQuotes"]
