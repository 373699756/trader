"""Read admitted company-replay evidence and compute descriptive pairs only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget, PredictionTargets
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.evaluation.company_replay import (
    CompanyReplayDay,
    CompanyReplayMetrics,
    EvaluateCompanyReplay,
)

CompanyReplayEvidenceKind = Literal["fixture", "point_in_time"]


@dataclass(frozen=True)
class CompanyReplayReadiness:
    evidence_kind: CompanyReplayEvidenceKind | None
    profile: ScoringProfileId | None
    target: PredictionTarget | None
    failure_reasons: tuple[str, ...]
    plan_sha256: str | None = None


class CompanyReplayEvidencePort(Protocol):
    def inspect(self) -> CompanyReplayReadiness: ...

    def read_mature_days(
        self, *, readiness: CompanyReplayReadiness, evaluation_at: datetime,
    ) -> tuple[CompanyReplayDay, ...] | None: ...


@dataclass(frozen=True)
class CompanyReplayReport:
    status: Literal["historical_data_insufficient", "fixture_diagnostics_completed", "fixture_diagnostics_partial"]
    evidence_kind: CompanyReplayEvidenceKind | None
    profile: ScoringProfileId | None
    strategy: Strategy | None
    target_bps: int | None
    failure_reasons: tuple[str, ...]
    metrics: CompanyReplayMetrics | None = None


def ExecuteCompanyReplay(
    evidence: CompanyReplayEvidencePort, targets: PredictionTargets, *, evaluation_at: datetime,
) -> CompanyReplayReport:
    readiness = evidence.inspect()
    profile, target = readiness.profile, readiness.target
    strategy = target.strategy if target is not None else None
    target_bps = target.threshold_bps if target is not None else None
    if readiness.failure_reasons:
        return CompanyReplayReport(
            "historical_data_insufficient",
            readiness.evidence_kind,
            profile,
            strategy,
            target_bps,
            readiness.failure_reasons,
        )
    if profile is None or target is None or targets.for_profile(profile, target.strategy) != target:
        raise ValueError("company replay prediction target differs from the configured profile and strategy")
    days = evidence.read_mature_days(readiness=readiness, evaluation_at=evaluation_at)
    if days is None:
        return CompanyReplayReport(
            "historical_data_insufficient",
            readiness.evidence_kind,
            profile,
            strategy,
            target_bps,
            ("mature_replay_evidence_unavailable",),
        )
    metrics = EvaluateCompanyReplay(days)
    incomplete = any(
        item.known_return_days < len(metrics.trade_dates)
        or min(item.known_targets, item.known_risk_outcomes, item.known_entries, item.known_exits) < item.seats
        for item in metrics.seats
    )
    return CompanyReplayReport(
        "fixture_diagnostics_partial" if incomplete else "fixture_diagnostics_completed",
        readiness.evidence_kind,
        profile,
        strategy,
        target_bps,
        ("mature_outcome_fields_incomplete",) if incomplete else (),
        metrics,
    )


__all__ = [
    "CompanyReplayEvidenceKind",
    "CompanyReplayEvidencePort",
    "CompanyReplayReadiness",
    "CompanyReplayReport",
    "ExecuteCompanyReplay",
]
