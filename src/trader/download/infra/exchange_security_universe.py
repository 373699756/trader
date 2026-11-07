"""Current supported A-share identities shared by history and qfq downloads."""

from __future__ import annotations

from typing import cast
from collections.abc import Callable
from trader.download.application.progress_message import ProgressField, ProgressMessage, ProgressTask

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockSecurity
from trader.download.domain.history_reference import HistoryStEvidence
from trader.download.domain.security_eligibility import IsStSecurityName
from trader.infra.market_data.observations import ListingFetcher


def LoadCurrentAShareUniverse(
    sse_fetcher: ListingFetcher, szse_fetcher: ListingFetcher, timeout_seconds: float,
    *, source_label: str, report: Callable[[ProgressMessage], None] | None = None,
) -> tuple[BaoStockSecurity, ...]:
    if not source_label.strip():
        raise ValueError("security_universe_source_invalid")
    task = ProgressTask("参考", "universe", "股票总体")
    if report is not None:
        report(ProgressMessage((ProgressField(f"来源 {source_label}"),), task=task, state="开始"))
    listings = tuple(sse_fetcher(timeout_seconds)) + tuple(szse_fetcher(timeout_seconds))
    if (
        len(listings) < 4000
        or len({item.code for item in listings}) != len(listings)
        or {item.exchange for item in listings} != {"SSE", "SZSE"}
        or any(item.board not in ("main", "chinext", "star") for item in listings)
    ):
        raise ValueError("official A-share security universe is incomplete")
    if report is not None:
        report(ProgressMessage((ProgressField(f"总体 {len(listings)} 股"), ProgressField(f"来源 {source_label}")),
                              task=task, state="完成", finished=True))
    return tuple(
        BaoStockSecurity(
            item.code, item.name, cast(BaoStockBoard, item.board), item.listing_date, None, "exchange_security_master"
        )
        for item in sorted(listings, key=lambda item: item.code)
    )


def SelectStEligibleUniverse(
    universe: tuple[BaoStockSecurity, ...], evidence: tuple[HistoryStEvidence, ...]
) -> tuple[BaoStockSecurity, ...]:
    """Apply the current and historical ST rule to every download population."""

    official_codes = {security.code for security in universe}
    current = {item.code: item for item in evidence if item.code in official_codes}
    if not official_codes or set(current) != official_codes:
        raise RuntimeError("historical ST evidence is unavailable or conflicts with the official universe")
    eligible = frozenset(
        security.code
        for security in universe
        if current[security.code].status == "clear" and not IsStSecurityName(security.name)
    )
    if not eligible:
        raise RuntimeError("historical ST eligible universe is empty")
    return tuple(security for security in universe if security.code in eligible)


__all__ = ["LoadCurrentAShareUniverse", "SelectStEligibleUniverse"]
