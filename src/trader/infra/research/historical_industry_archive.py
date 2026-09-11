"""Read-only historical-industry qualification over the active monthly archive."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path

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
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.history_archive_status import (
    HistoryArchiveError,
    load_active_history_archive,
    verify_active_history_archive,
)
from trader.infra.research.history_month_archive import SQLiteHistoryMonthlyArchive

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
    revisions = tuple(SQLiteHistoryMonthlyArchive(archive.root).iter_snapshot_revisions(archive.snapshot))
    if not revisions:
        raise HistoryArchiveError("history_snapshot_rows_unavailable")
    facts = _industry_facts(revisions, archive.snapshot.content_hash)
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
    revisions: tuple[HistoryMonthlyRevision, ...],
    source_version: str,
) -> tuple[HistoricalIndustryFact, ...]:
    grouped: dict[str, list[HistoryMonthlyRevision]] = defaultdict(list)
    for revision in revisions:
        if revision.industry is not None and revision.industry_classification is not None:
            grouped[revision.code].append(revision)
    facts: list[HistoricalIndustryFact] = []
    for code, values in sorted(grouped.items()):
        ordered = sorted(values, key=lambda item: item.trade_date)
        starts = [
            index
            for index, value in enumerate(ordered)
            if index == 0
            or (value.industry, value.industry_classification)
            != (ordered[index - 1].industry, ordered[index - 1].industry_classification)
        ]
        for offset, index in enumerate(starts):
            value = ordered[index]
            facts.append(
                HistoricalIndustryFact(
                    "baostock_archived_industry",
                    source_version,
                    code,
                    value.industry or "",
                    value.industry_classification or "",
                    value.trade_date,
                    ordered[starts[offset + 1]].trade_date if offset + 1 < len(starts) else None,
                    None,
                    value.content_hash,
                )
            )
    return tuple(facts)


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
