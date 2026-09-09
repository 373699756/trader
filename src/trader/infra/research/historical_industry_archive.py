"""Read-only historical-industry qualification over the sealed BaoStock archive."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from trader.domain.research.baostock_daily import BaoStockDailySpec, BaoStockIndustryInterval, BaoStockSecurity
from trader.domain.research.historical_industry_facts import (
    HistoricalIndustryCohort,
    HistoricalIndustryDatasetReport,
    HistoricalIndustryFact,
    HistoricalIndustrySourceContract,
    HistoricalIndustryStockWindow,
    build_historical_industry_dataset_report,
    build_historical_industry_source_audit,
    merge_historical_industry_facts,
)
from trader.infra.research.baostock_daily import BaoStockDailyArtifactConflictError, SQLiteBaoStockDailyShard
from trader.infra.research.baostock_partition_archive import BaoStockDailyPartitionedArchive

_TUSHARE_HISTORICAL_INDUSTRY_MINIMUM_POINTS = 2000


def audit_archived_historical_industry_facts(
    history_root: Path,
    *,
    required_sample_codes: int = 300,
    tushare_access_points: int = 0,
) -> HistoricalIndustryDatasetReport:
    """Audit source capability without changing the frozen daily partitions."""

    root = history_root / "baostock-daily" / "sessions-2000"
    manifest = BaoStockDailyPartitionedArchive(root).verify()
    spec = BaoStockDailySpec(sessions=2000)
    shards = tuple(SQLiteBaoStockDailyShard(root / item.relative_path) for item in manifest.partitions)
    if not shards:
        raise BaoStockDailyArtifactConflictError("BaoStock industry audit has no sealed partitions")
    context = shards[0].context(spec)
    if context is None:
        raise BaoStockDailyArtifactConflictError("BaoStock industry audit context is missing")
    expected_intervals = _interval_identity(context.industry_intervals)
    for shard in shards[1:]:
        candidate = shard.context(spec)
        if candidate is None or _interval_identity(candidate.industry_intervals) != expected_intervals:
            raise BaoStockDailyArtifactConflictError("BaoStock industry facts differ across sealed partitions")

    windows = tuple(_stock_window(item, context.calendar.open_dates) for item in context.universe)
    facts = tuple(
        HistoricalIndustryFact(
            source="baostock_archived_industry",
            source_version=context.source_versions.sdk_version,
            code=item.code,
            industry=item.industry,
            classification=item.classification,
            effective_from=item.effective_from,
            effective_to=item.effective_to,
            queried_at=None,
            evidence_hash=item.content_hash,
        )
        for item in context.industry_intervals
    )
    baostock = build_historical_industry_source_audit(
        HistoricalIndustrySourceContract(
            "baostock_archived_industry",
            context.source_versions.sdk_version,
            code_available=True,
            industry_available=True,
            classification_available=True,
            effective_from_available=True,
            effective_to_available=True,
            queried_at_available=False,
            source_identity_available=True,
        ),
        facts,
        windows,
        required_sample_codes=required_sample_codes,
    )
    tushare_reason = (
        "source_access_below_required_points"
        if tushare_access_points < _TUSHARE_HISTORICAL_INDUSTRY_MINIMUM_POINTS
        else "source_not_sampled"
    )
    tushare = build_historical_industry_source_audit(
        HistoricalIndustrySourceContract(
            "tushare_index_member_all",
            "document_335",
            code_available=True,
            industry_available=True,
            classification_available=True,
            effective_from_available=True,
            effective_to_available=True,
            queried_at_available=True,
            source_identity_available=True,
            unavailable_reason=tushare_reason,
        ),
        (),
        (),
        required_sample_codes=required_sample_codes,
    )
    return build_historical_industry_dataset_report(
        manifest.content_hash,
        (baostock, tushare),
        merge_historical_industry_facts(facts),
    )


def _stock_window(security: BaoStockSecurity, open_dates: tuple[date, ...]) -> HistoricalIndustryStockWindow:
    expected = tuple(
        day
        for day in open_dates
        if day >= security.listed_on and (security.delisted_on is None or day < security.delisted_on)
    )
    if not expected:
        raise BaoStockDailyArtifactConflictError("BaoStock industry stock has no actual trade dates")
    recent_boundary = open_dates[-min(252, len(open_dates))]
    cohort: HistoricalIndustryCohort
    if security.delisted_on is not None and security.delisted_on <= open_dates[-1]:
        cohort = "delisted"
    elif security.listed_on >= recent_boundary:
        cohort = "new"
    else:
        cohort = "old"
    return HistoricalIndustryStockWindow(security.code, security.board, cohort, expected)


def _interval_identity(values: tuple[BaoStockIndustryInterval, ...]) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            item.code,
            item.effective_from,
            item.effective_to,
            item.industry,
            item.classification,
        )
        for item in values
    )


__all__ = ["audit_archived_historical_industry_facts"]
