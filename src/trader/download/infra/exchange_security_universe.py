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


def select_history_eligible_universe(
    universe: tuple[BaoStockSecurity, ...], eligible_codes: tuple[str, ...]
) -> tuple[BaoStockSecurity, ...]:
    """Bind a current official universe to the active history training eligibility."""

    official_codes = {security.code for security in universe}
    eligible = frozenset(eligible_codes)
    if not eligible or not eligible.issubset(official_codes):
        raise RuntimeError("history eligible universe is unavailable or conflicts with the official universe")
    return tuple(security for security in universe if security.code in eligible)


__all__ = ["load_current_a_share_universe", "select_history_eligible_universe"]
