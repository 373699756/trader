"""Typed persistence boundary for production-isolated Tomorrow profile evidence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from trader.domain.outcome.models import RecommendationOutcome
from trader.domain.research.tomorrow_profile_comparison import (
    TomorrowProfileComparisonReport,
    TomorrowProfileComparisonStatus,
    TomorrowProfilePair,
    TomorrowProfilePairManifest,
)


@dataclass(frozen=True)
class TomorrowFormalPairTarget:
    record_version: str
    input_version: str
    trade_date: date
    pair: TomorrowProfilePair


class TomorrowProfileEvidencePort(Protocol):
    def initialize(self) -> None: ...

    def save_manifest(self, manifest: TomorrowProfilePairManifest) -> None: ...

    def load_manifest(self, input_version: str) -> TomorrowProfilePairManifest | None: ...

    def bind_formal_input(
        self,
        *,
        trade_date: date,
        input_version: str,
        record_version: str,
        committed_at: datetime,
    ) -> None: ...

    def pending_formal_targets(self, *, limit: int) -> Sequence[TomorrowFormalPairTarget]: ...

    def save_outcomes(self, outcomes: Sequence[RecommendationOutcome]) -> None: ...

    def settled_outcomes(self) -> Sequence[RecommendationOutcome]: ...

    def complete_outcomes(self) -> Sequence[RecommendationOutcome]: ...

    def formal_manifests(self) -> Sequence[TomorrowProfilePairManifest]: ...

    def status(self) -> TomorrowProfileComparisonStatus: ...

    def save_terminal_report(self, report: TomorrowProfileComparisonReport) -> None: ...


__all__ = ["TomorrowFormalPairTarget", "TomorrowProfileEvidencePort"]
