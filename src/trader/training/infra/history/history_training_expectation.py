"""Explicit current inputs for read-only training maintenance checks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from trader.download.domain.current_industry import CurrentIndustrySnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.application.resources import TrainingProfileThreads
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.training_contract import BuildTrainingContractHash
from trader.training.infra.training_settings import ProfileTrainingSettings


@dataclass(frozen=True)
class HistoryTrainingExpectation:
    industry_snapshot: CurrentIndustrySnapshot
    contracts: tuple[tuple[Strategy, str], ...]

    def __post_init__(self) -> None:
        if (
            tuple(strategy for strategy, _ in self.contracts) != (Strategy.TOMORROW, Strategy.D25)
            or any(len(value) != 64 or any(character not in "0123456789abcdef" for character in value)
                   for _, value in self.contracts)
        ):
            raise ValueError("training_maintenance_expectation_invalid")

    def contract_hash(self, strategy: Strategy) -> str:
        return next(value for head, value in self.contracts if head is strategy)


@dataclass(frozen=True)
class HistoryTrainingExpectationReader:
    load_industry_snapshot: Callable[[datetime], CurrentIndustrySnapshot]
    load_settings: Callable[[ScoringProfileId], ProfileTrainingSettings]
    load_profile_threads: Callable[[], TrainingProfileThreads]

    def read(self, profile: TrainedProfileContract, observed_at: datetime) -> HistoryTrainingExpectation:
        settings = self.load_settings(profile.profile_id)
        settings.require_profile(profile)
        industry = self.load_industry_snapshot(observed_at)
        if not industry.observed_at <= observed_at < industry.refresh_after:
            raise ValueError("current_industry_snapshot_expired")
        compute_threads = (
            settings.fitting.compute_threads if isinstance(settings.fitting, TcnFittingParameters)
            else self.load_profile_threads().for_profile(profile.profile_id)
        )
        return HistoryTrainingExpectation(
            industry,
            tuple((head.strategy, BuildTrainingContractHash(
                profile, head, settings, compute_threads=compute_threads,
            )) for head in profile.heads),
        )


__all__ = [
    "HistoryTrainingExpectation",
    "HistoryTrainingExpectationReader",
]
