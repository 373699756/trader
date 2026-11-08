"""Explicit maintenance inputs for isolated publication and command fixtures."""

from datetime import datetime

from tests.helpers.training.current_industry import CurrentIndustrySnapshotFixture
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.history.history_training_expectation import HistoryTrainingExpectation
from trader.training.infra.model_bundles.contracts import TrainedProfileContract


def HistoryTrainingExpectationFixture(
    _profile: TrainedProfileContract, observed_at: datetime,
) -> HistoryTrainingExpectation:
    return HistoryTrainingExpectation(
        CurrentIndustrySnapshotFixture(("600001",), observed_at=observed_at),
        ((Strategy.TOMORROW, "9" * 64), (Strategy.D25, "9" * 64)),
    )
