"""Bind disclosed official industry intervals while retaining CNINFO candidate diagnostics."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, nullcontext
from dataclasses import dataclass, replace
from datetime import date, datetime
import re

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.domain.historical_industry import BuildHistoricalIndustryPreparation, HistoricalIndustryPreparation
from trader.download.domain.history_sync import HistorySyncProgress, HistorySyncProgressPort
from trader.download.infra.cninfo_industry import CninfoIndustryError, ParseCninfoIndustryChanges
from trader.download.infra.industry_query_index import IndustrySourceDocumentRead, SQLiteIndustryQueryIndex


@dataclass(frozen=True)
class _CandidateInspection:
    codes: frozenset[str]
    failures: tuple[str, ...]


def PrepareHistoricalIndustries(
    index: SQLiteIndustryQueryIndex | None,
    universe: tuple[BaoStockSecurity, ...],
    calendar_dates: tuple[date, ...],
    observed_at: datetime,
    *,
    cancel_requested: Callable[[], bool],
    progress: HistorySyncProgressPort | None = None,
) -> HistoricalIndustryPreparation:
    if not calendar_dates:
        raise ValueError("historical_industry_window_invalid")
    if cancel_requested():
        raise RuntimeError("supplier_cancelled")
    # Official source corruption remains fatal; diagnostic CNINFO failures cannot erase valid official facts.
    evidence = index.read_evidence_window(calendar_dates[0], calendar_dates[-1], observed_at) if index else ()
    candidates = _inspect_candidate_codes(
        index,
        universe,
        calendar_dates[0],
        calendar_dates[-1],
        observed_at,
        cancel_requested=cancel_requested,
        progress=progress,
    )
    if cancel_requested():
        raise RuntimeError("supplier_cancelled")
    prepared = BuildHistoricalIndustryPreparation(universe, calendar_dates, observed_at, evidence, candidates.codes)
    if candidates.failures:
        prepared = replace(prepared, readiness=replace(
            prepared.readiness,
            failure_category=f"cninfo_candidate_failures:{len(candidates.failures)}:{candidates.failures[0]}",
        ))
    if progress is not None:
        progress.publish(HistorySyncProgress(
            "supplier_industry", "completed", len(universe), len(universe), "capco",
            industry_missing_codes=len(universe) - prepared.readiness.point_in_time_codes,
        ))
    return prepared


def _inspect_candidate_codes(
    index: SQLiteIndustryQueryIndex | None,
    universe: tuple[BaoStockSecurity, ...],
    first_date: date,
    last_date: date,
    observed_at: datetime,
    *,
    cancel_requested: Callable[[], bool],
    progress: HistorySyncProgressPort | None,
) -> _CandidateInspection:
    candidates: set[str] = set()
    failures: list[str] = []
    keys = tuple("cninfo:p_stock2110:" + security.code for security in universe)
    if not keys:
        return _CandidateInspection(frozenset(), ())
    scope = (
        closing(index.iter_source_documents(keys, observed_at, cancel_requested=cancel_requested))
        if index is not None else nullcontext(IndustrySourceDocumentRead(key, None) for key in keys)
    )
    with scope as reads:
        for position, (security, result) in enumerate(zip(universe, reads, strict=True), start=1):
            if cancel_requested():
                raise RuntimeError("supplier_cancelled")
            document = result.document
            reason = result.failure_category
            if document is not None:
                try:
                    changes = ParseCninfoIndustryChanges(
                        document.content, security.code, document.requested_from, document.requested_to,
                        cancel_requested=cancel_requested,
                    )
                except (CninfoIndustryError, ValueError) as exc:
                    reason = str(exc)
                    if re.fullmatch(r"(?:cninfo|industry_source)_[a-z_]{1,100}", reason) is None:
                        reason = type(exc).__name__
                else:
                    if document.requested_from <= max(first_date, security.listed_on) and document.requested_to >= last_date:
                        if any(item.is_current_standard for item in changes.rows):
                            candidates.add(security.code)
            if reason is not None:
                failures.append(f"{security.code}:{reason}")
            if progress is not None:
                if reason is not None:
                    progress.publish(HistorySyncProgress(
                        "supplier_industry", "failed", position, len(universe), security.code,
                        failure_reason="cninfo_candidate_invalid", failure_detail=reason,
                    ))
                else:
                    progress.publish(HistorySyncProgress(
                        "supplier_industry", "completed", position, len(universe), security.code,
                        cache_hit=True if document is not None else None,
                    ))
    return _CandidateInspection(frozenset(candidates), tuple(failures))


__all__ = ["PrepareHistoricalIndustries"]
