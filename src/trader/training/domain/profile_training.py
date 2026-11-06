"""Pure production training plans, without model files or native fitting state."""

from dataclasses import dataclass
from datetime import date
from typing import Literal

from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.final_fit import BuildFinalFitDatePlan, FinalFitDatePlan
from trader.training.domain.production_diagnostic_dates import BuildProductionDiagnosticDatePlan, ProductionDiagnosticDatePlan
from trader.training.domain.tomorrow_training_input import FrozenDailyInputDescriptor


@dataclass(frozen=True)
class ProfileTrainingHead:
    strategy: Strategy
    maturity_sessions: int
    training_contract_hash: str


@dataclass(frozen=True)
class ProfileTrainingSource:
    profile_id: ScoringProfileId
    algorithm: Literal["lightgbm", "ridge", "tcn"]
    calendar: tuple[date, ...]
    source_cutoff: date
    snapshot_hash: str
    input_scope: Literal["complete_manifest"]
    training_codes: int
    universe_codes: int
    fit_sessions: int
    history_sessions: int
    minimum_industry_training_rows: int
    heads: tuple[ProfileTrainingHead, ...]
    descriptor: FrozenDailyInputDescriptor
    target_matches: bool = True

    def __post_init__(self) -> None:
        if (
            tuple((head.strategy, head.maturity_sessions) for head in self.heads)
            != ((Strategy.TOMORROW, 1), (Strategy.D25, 5))
            or (self.profile_id, self.algorithm) not in {("v2", "lightgbm"), ("v3", "ridge"), ("v5", "tcn")}
            or type(self.minimum_industry_training_rows) is not int or self.minimum_industry_training_rows < 1
        ):
            raise ValueError("profile_training_source_contract_invalid")


@dataclass(frozen=True)
class PlannedTrainingHead:
    strategy: Strategy
    label_cutoff: date
    training_contract_hash: str
    final_fit: FinalFitDatePlan


@dataclass(frozen=True)
class ProfileTrainingPlan:
    source: ProfileTrainingSource
    split: ProductionDiagnosticDatePlan
    heads: tuple[PlannedTrainingHead, ...]
    run_id: str


def BuildProfileDiagnosticPlan(
    calendar: tuple[date, ...],
    *,
    source_cutoff: date,
    fit_sessions: int,
    history_sessions: int,
    algorithm: Literal["lightgbm", "ridge", "tcn"],
) -> ProductionDiagnosticDatePlan:
    return BuildProductionDiagnosticDatePlan(
        calendar,
        source_cutoff=source_cutoff,
        fit_sessions=fit_sessions,
        early_stopping_sessions=20 if algorithm in {"lightgbm", "tcn"} else 0,
        history_sessions=history_sessions,
        history_excludes_signal=algorithm == "tcn",
    )


def BuildProfileTrainingPlan(source: ProfileTrainingSource) -> ProfileTrainingPlan:
    split = BuildProfileDiagnosticPlan(
        source.calendar,
        source_cutoff=source.source_cutoff,
        fit_sessions=source.fit_sessions,
        history_sessions=source.history_sessions,
        algorithm=source.algorithm,
    )
    position = source.calendar.index(source.source_cutoff)
    if any(position < head.maturity_sessions for head in source.heads):
        raise ValueError("history_training_data_incomplete")
    heads = tuple(
        PlannedTrainingHead(
            head.strategy,
            source.calendar[position - head.maturity_sessions],
            head.training_contract_hash,
            BuildFinalFitDatePlan(
                source.calendar,
                source_cutoff=source.source_cutoff,
                fit_sessions=source.fit_sessions,
                maturity_sessions=head.maturity_sessions,
            ),
        )
        for head in source.heads
    )
    run_id = CanonicalDigest((
        source.profile_id,
        source.snapshot_hash,
        split.content_hash,
        tuple((head.strategy.value, head.training_contract_hash) for head in heads),
    ))
    return ProfileTrainingPlan(source, split, heads, run_id)


__all__ = [
    "BuildProfileDiagnosticPlan",
    "BuildProfileTrainingPlan",
    "PlannedTrainingHead",
    "ProfileTrainingHead",
    "ProfileTrainingPlan",
    "ProfileTrainingSource",
]
