"""Diagnostic-only Eastmoney spot probe; this endpoint has no production lane."""

from __future__ import annotations

import math
import time
from concurrent.futures import as_completed
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.bootstrap import OpenExternalHttp
from trader.infra.http_contracts import HttpError
from trader.infra.workers import BoundedExecutor, SubmitOrReject
from trader.recommendation.domain.market.models import MarketQuote
from trader.recommendation.infra.market_data.providers.eastmoney_spot import EastmoneySpotClient


def _page(number: int, timeout: float, deadline: float) -> tuple[tuple[MarketQuote, ...], int]:
    last_error: Exception | None = None
    for host in ("82.push2.eastmoney.com", "push2.eastmoney.com", "7.push2.eastmoney.com"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("eastmoney diagnostic deadline")
        try:
            with OpenExternalHttp() as session:
                page = EastmoneySpotClient(session).fetch_page(
                    number,
                    host=host,
                    timeout=min(timeout, remaining),
                    deadline=deadline,
                    received_at=datetime.now(ZoneInfo("Asia/Shanghai")),
                )
            return page.quotes, page.total
        except (HttpError, OSError, ValueError) as exc:
            last_error = exc
    raise RuntimeError("eastmoney diagnostic request failed") from last_error


def FetchEastmoneyQuotes(timeout: float, deadline: float, pool: BoundedExecutor) -> tuple[MarketQuote, ...]:
    first, total = _page(1, timeout, deadline)
    pages = math.ceil(total / EastmoneySpotClient.PAGE_SIZE)
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


__all__ = ["FetchEastmoneyQuotes"]
