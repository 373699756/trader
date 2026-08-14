"""Read ports for the offline Score-R2 evidence boundary."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from trader.application.ports.market import DataPlaneReadPort
from trader.application.research.challenger_models import ChallengerCandidateOverride, ChallengerReplaySelection
from trader.application.research.models import (
    HistoricalDaySummary,
    HistoricalEvaluatedCandidate,
    HistoricalExtractedDay,
    HistoricalFullFieldBundle,
)
from trader.application.research.replay_models import BaselineReplaySelection
from trader.domain.research.challengers import ChallengerSpecification


class HistoricalDataPlaneReadPort(DataPlaneReadPort, Protocol):
    """Offline extension of the canonical E1 read port for Score-R2 adapters.

    Implementations retain the canonical immutable snapshot boundary and must
    discard hard-reject identities when projecting historical research data.
    """

    def is_trading_day(self, trade_date: date) -> bool: ...

    def read_day_summary(self, trade_date: date) -> HistoricalDaySummary: ...

    def load_full_fields(
        self,
        trade_date: date,
        codes: tuple[str, ...],
    ) -> HistoricalFullFieldBundle: ...


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
]
