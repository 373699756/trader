"""Application wrapper for one-shot historical candidate confirmation."""

from __future__ import annotations

from trader.domain.research.historical_candidate_confirmation import (
    CandidateConfirmationSeries,
    HistoricalCandidateConfirmationReport,
    confirm_transparent_candidates,
)
from trader.domain.research.transparent_candidate import TransparentCandidateFamily


def execute_historical_candidate_confirmation(
    family: TransparentCandidateFamily, series: tuple[CandidateConfirmationSeries, ...]
) -> HistoricalCandidateConfirmationReport:
    return confirm_transparent_candidates(family, series)


__all__ = ["execute_historical_candidate_confirmation"]
