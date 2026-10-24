"""Immutable successful fits, rejected industries and actual prediction coverage."""

from dataclasses import dataclass

from trader.training.infra.model_bundles.bundle_codec import (
    TrainedLightGBMModelSnapshot,
    TrainedRidgeModelSnapshot,
    TrainedTcnModelSnapshot,
)
from trader.training.infra.model_evaluation import CalibrationResult, PredictionMetrics

FittedModelSnapshot = TrainedLightGBMModelSnapshot | TrainedRidgeModelSnapshot | TrainedTcnModelSnapshot


@dataclass(frozen=True)
class FittedIndustryModel:
    industry: str
    model: FittedModelSnapshot
    training_rows: int
    calibration: CalibrationResult


@dataclass(frozen=True)
class IndustryFitFailure:
    industry: str
    reason: str
    training_rows: int
    calibration_rows: int
    calibration_dates: int
    validation_rows: int


@dataclass(frozen=True)
class IndustryFitResult:
    models: tuple[FittedIndustryModel, ...]
    failures: tuple[IndustryFitFailure, ...]
    metrics: PredictionMetrics
    industry_metrics: tuple[tuple[str, PredictionMetrics], ...]
    candidate_training_rows: int
    sealed_holdout_rows: int

    @property
    def training_rows(self) -> int:
        return sum(item.training_rows for item in self.models)

    @property
    def validation_rows(self) -> int:
        return self.metrics.predicted_rows


__all__ = ["FittedModelSnapshot", "FittedIndustryModel", "IndustryFitFailure", "IndustryFitResult"]
