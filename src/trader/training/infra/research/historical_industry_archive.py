"""Read-only historical-industry qualification over the active monthly archive."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.infra.history_archive_reader import SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_status import (
    HistoryArchiveError,
    load_active_history_archive,
    verify_active_history_archive,
)
from trader.training.domain.evaluation.historical_industry_facts import (
    HistoricalIndustryCohort,
    HistoricalIndustryDatasetReport,
    HistoricalIndustryFact,
    HistoricalIndustrySourceContract,
    HistoricalIndustryStockWindow,
    build_historical_industry_dataset_report,
    build_historical_industry_source_audit,
    merge_historical_industry_facts,
)

_TUSHARE_HISTORICAL_INDUSTRY_MINIMUM_POINTS = 2000


def audit_archived_historical_industry_facts(
    history_root: Path,
    *,
    required_sample_codes: int = 300,
    tushare_access_points: int = 0,
) -> HistoricalIndustryDatasetReport:
    """Audit the immutable active snapshot without changing its partitions."""

    archive = load_active_history_archive(history_root)
    verify_active_history_archive(archive)
    reference = SQLiteHistoryArchiveReader(archive.root).reference_index(archive.snapshot).reference
    facts = _industry_facts(reference, archive.snapshot.content_hash)
    windows = tuple(
        _stock_window(item.code, item.board, item.listed_on, item.delisted_on, archive.calendar.open_dates)
        for item in archive.universe.securities
    )
    baostock = build_historical_industry_source_audit(
        HistoricalIndustrySourceContract(
            "baostock_archived_industry",
            archive.snapshot.content_hash,
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
        archive.snapshot.content_hash,
        (baostock, tushare),
        merge_historical_industry_facts(facts),
    )


def _industry_facts(
    reference: HistoryReferenceSnapshot,
    source_version: str,
) -> tuple[HistoricalIndustryFact, ...]:
    return tuple(
        HistoricalIndustryFact(
            "baostock_archived_industry",
            source_version,
            item.code,
            item.industry,
            item.classification,
            item.effective_from,
            item.effective_to,
            None,
            item.content_hash,
        )
        for item in reference.industry_intervals
    )


def _stock_window(
    code: str,
    board: str,
    listed_on: date,
    delisted_on: date | None,
    open_dates: tuple[date, ...],
) -> HistoricalIndustryStockWindow:
    expected = tuple(day for day in open_dates if day >= listed_on and (delisted_on is None or day < delisted_on))
    if not expected:
        raise HistoryArchiveError("history_industry_stock_window_unavailable")
    recent_boundary = open_dates[-min(252, len(open_dates))]
    cohort: HistoricalIndustryCohort
    if delisted_on is not None and delisted_on <= open_dates[-1]:
        cohort = "delisted"
    elif listed_on >= recent_boundary:
        cohort = "new"
    else:
        cohort = "old"
    return HistoricalIndustryStockWindow(code, board, cohort, expected)  # type: ignore[arg-type]


__all__ = ["audit_archived_historical_industry_facts"]
