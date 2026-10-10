"""Current supported A-share identities shared by history and qfq downloads."""

from __future__ import annotations

from typing import cast

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.infra.market_data.providers.exchange_security_master import ListingFetcher


def load_current_a_share_universe(
    sse_fetcher: ListingFetcher, szse_fetcher: ListingFetcher, timeout_seconds: float
) -> tuple[BaoStockSecurity, ...]:
    listings = tuple(sse_fetcher(timeout_seconds)) + tuple(szse_fetcher(timeout_seconds))
    if (
        len(listings) < 4000
        or len({item.code for item in listings}) != len(listings)
        or {item.exchange for item in listings} != {"SSE", "SZSE"}
        or any(item.board not in ("main", "chinext", "star") for item in listings)
    ):
        raise ValueError("official A-share security universe is incomplete")
    return tuple(
        BaoStockSecurity(
            item.code, item.name, cast(BaoStockBoard, item.board), item.listing_date, None, "exchange_security_master"
        )
        for item in sorted(listings, key=lambda item: item.code)
    )
