"""Read-only historical-industry qualification over the published monthly history."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

from datetime import date
from pathlib import Path

from trader.download.domain.history_reference import HistoryReferenceSnapshot
from trader.download.infra.history_snapshot_inspection import (
    HistorySnapshotInspectionError,
    LoadPublishedHistorySnapshot,
    VerifyPublishedHistorySnapshot,
)
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.training.domain.evaluation.historical_industry_facts import (
    BuildHistoricalIndustryDatasetReport,
    BuildHistoricalIndustrySourceAudit,
    HistoricalIndustryCohort,
    HistoricalIndustryDatasetReport,
    HistoricalIndustryFact,
    HistoricalIndustrySourceContract,
    HistoricalIndustryStockWindow,
    MergeHistoricalIndustryFacts,
)

_TUSHARE_HISTORICAL_INDUSTRY_MINIMUM_POINTS = 2000


def AuditPublishedHistoricalIndustryFacts(
    history_root: Path,
    *,
    required_sample_codes: int = 300,
    tushare_access_points: int = 0, sqlite_policy: SqliteIoPolicy,
) -> HistoricalIndustryDatasetReport:
    """Audit the immutable published snapshot without changing its partitions."""

    history = LoadPublishedHistorySnapshot(history_root, sqlite_policy=sqlite_policy)
    VerifyPublishedHistorySnapshot(history, sqlite_policy=sqlite_policy)
    reference = HistorySnapshotReader(history.root, sqlite_policy=sqlite_policy).reference_index(history.snapshot).reference
    facts = _industry_facts(reference, history.snapshot.content_hash)
    windows = tuple(
        _stock_window(item.code, item.board, item.listed_on, item.delisted_on, history.calendar.open_dates)
        for item in history.universe.securities
    )
    baostock = BuildHistoricalIndustrySourceAudit(
        HistoricalIndustrySourceContract(
            "baostock_archived_industry",
            history.snapshot.content_hash,
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
    tushare = BuildHistoricalIndustrySourceAudit(
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
    return BuildHistoricalIndustryDatasetReport(
        history.snapshot.content_hash,
        (baostock, tushare),
        MergeHistoricalIndustryFacts(facts),
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
        raise HistorySnapshotInspectionError("history_industry_stock_window_unavailable")
    recent_boundary = open_dates[-min(252, len(open_dates))]
    cohort: HistoricalIndustryCohort
    if delisted_on is not None and delisted_on <= open_dates[-1]:
        cohort = "delisted"
    elif listed_on >= recent_boundary:
        cohort = "new"
    else:
        cohort = "old"
    return HistoricalIndustryStockWindow(code, board, cohort, expected)  # type: ignore[arg-type]


__all__ = ["AuditPublishedHistoricalIndustryFacts"]
