"""Read ports for the offline Historical extraction evidence boundary."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Protocol

from trader.recommendation.domain.market.data_plane import MarketDataPlaneSnapshot
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.market.refresh import ResearchRefreshResult
from trader.training.application.baseline_replay_report import BaselineReplaySelection
from trader.training.application.challenger_replay_report import (
    ChallengerCandidateOverride,
    ChallengerReplaySelection,
)
from trader.training.application.historical_extraction_models import (
    HistoricalDaySummary,
    HistoricalEvaluatedCandidate,
    HistoricalExtractedDay,
    HistoricalFullFieldBundle,
)
from trader.training.domain.evaluation.challengers import ChallengerSpecification


class HistoricalDataPlaneReadPort(Protocol):
    """Offline extension of the canonical historical read port for Historical extraction adapters.

    Implementations retain the canonical immutable snapshot boundary and must
    discard hard-reject identities when projecting historical research data.
    """

    def snapshot(self) -> MarketDataPlaneSnapshot: ...

    def is_trading_day(self, trade_date: date) -> bool: ...

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary: ...

    def load_full_fields(
        self,
        trade_date: date,
        codes: tuple[str, ...],
    ) -> HistoricalFullFieldBundle: ...


class OfflineResearchReaderPort(Protocol):
    def refresh_industry_heat(self, observed_at: datetime) -> Sequence[FeatureSnapshot]: ...

    def refresh_market_news(
        self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None
    ) -> ResearchRefreshResult: ...

    def refresh_stock_risk(
        self, codes: Sequence[str], observed_at: datetime, *, deadline: datetime | None = None
    ) -> ResearchRefreshResult: ...


class HistoricalCandidateEvaluator(Protocol):
    """Adapter to the same pure production evaluator used by the later replay."""

    def evaluate(
        self,
        summary: HistoricalDaySummary,
        bundle: HistoricalFullFieldBundle,
    ) -> tuple[HistoricalEvaluatedCandidate, ...]: ...


class HistoricalBaselineReplayEvaluator(Protocol):
    """Adapter that invokes the same pure production selection used by the active baseline."""

    def replay(self, day: HistoricalExtractedDay) -> tuple[BaselineReplaySelection, ...]: ...


class HistoricalChallengerReplayEvaluator(Protocol):
    """Adapter to the production pure functions with one immutable research override."""

    def replay(
        self,
        day: HistoricalExtractedDay,
        specification: ChallengerSpecification,
        overrides: tuple[ChallengerCandidateOverride, ...],
    ) -> tuple[ChallengerReplaySelection, ...]: ...


__all__ = [
    "HistoricalBaselineReplayEvaluator",
    "HistoricalCandidateEvaluator",
    "HistoricalChallengerReplayEvaluator",
    "HistoricalDataPlaneReadPort",
    "OfflineResearchReaderPort",
]
