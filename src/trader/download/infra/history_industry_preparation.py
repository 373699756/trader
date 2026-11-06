"""Inspect CNINFO candidates without promoting change dates to disclosure evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.history_sync import HistoryIndustryReadiness, HistorySyncProgress, HistorySyncProgressPort
from trader.download.infra.cninfo_industry import ParseCninfoIndustryChanges
from trader.download.infra.industry_query_index import SQLiteIndustryQueryIndex


def InspectHistoricalIndustryQueries(
    index: SQLiteIndustryQueryIndex | None,
    universe: tuple[BaoStockSecurity, ...],
    first_date: date,
    last_date: date,
    observed_at: datetime,
    *,
    cancel_requested: Callable[[], bool],
    progress: HistorySyncProgressPort | None = None,
) -> HistoryIndustryReadiness:
    candidates = 0
    for position, security in enumerate(universe):
        if cancel_requested():
            raise RuntimeError("supplier_cancelled")
        document = index.read_source_document("cninfo:p_stock2110:" + security.code, observed_at) if index else None
        if document is not None:
            changes = ParseCninfoIndustryChanges(document.content, security.code, document.requested_from, document.requested_to)
            if document.requested_from <= max(first_date, security.listed_on) and document.requested_to >= last_date:
                candidates += int(any(item.is_current_standard for item in changes.rows))
        if progress is not None:
            progress.publish(HistorySyncProgress(
                "supplier_industry", "completed", position + 1, len(universe), security.code,
                cache_hit=True if document is not None else None, industry_missing_codes=position + 1,
            ))
    # VARYDATE is a classification change date. Neither it nor the download time
    # establishes when this classification was publicly available in the past.
    # Keep these originals for later evidence review; price publication may proceed.
    return HistoryIndustryReadiness(len(universe), candidates, 0)


__all__ = ["InspectHistoricalIndustryQueries"]
