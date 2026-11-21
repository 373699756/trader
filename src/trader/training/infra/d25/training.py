"""Fit threshold-specific D25 models and calibrate fixed weights out of sample."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
import numpy as np

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.production_diagnostic_dates import ProductionDiagnosticDatePlan
from trader.training.infra.d25.artifact import (
    D25Model,
    D25ProbabilityArtifact,
    D25ProbabilityBinding,
)
from trader.training.infra.d25.calibration import CalibrateD25Probability, HasD25ProbabilityLabels
from trader.training.infra.d25.v2.probability import FitD25Lightgbm
from trader.training.infra.d25.v3.probability import FitD25Logistic
from trader.training.infra.d25.v5.probability import FitD25Tcn
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.fitting_settings import LightGBMFittingParameters, ModelFittingParameters, RidgeFittingParameters
from trader.training.infra.tcn.settings import TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingSampleBatch, TrainingSampleMatrix


@dataclass(frozen=True, slots=True)
class D25IndustryFitRequest:
    profile: ScoringProfileId
    contract: TrainedHeadContract
    split: ProductionDiagnosticDatePlan
    parameters: ModelFittingParameters
    industries: tuple[str, ...]
    compute_threads: int
    binding: D25ProbabilityBinding


def FitIndustryD25Probability(
    samples: SQLiteTrainingSampleIndex, request: D25IndustryFitRequest, check: Callable[[], None],
) -> D25ProbabilityArtifact:
    models: list[tuple[str, D25Model]] = []
    scores: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    fitted_dates: set[date] = set()
    calibration_dates: set[date] = set()
    for industry in sorted(request.industries):
        check()
        training, dates = _read_training_matrix(samples.iter_batches(
            industry, request.contract, "training", d25_probability=True,
        ), check)
        if len(training.labels) < request.parameters.minimum_industry_training_rows or not HasD25ProbabilityLabels(training.labels):
            continue
        model, logits = _fit_industry(training, request.parameters, request.compute_threads, check)
        for batch in samples.iter_batches(industry, request.contract, "calibration", d25_probability=True):
            check()
            scores.append(logits(batch.features))
            labels.append(batch.labels)
            calibration_dates.update(batch.trade_dates)
        models.append((industry, model))
        fitted_dates.update(dates)
    if not models or not scores:
        raise ValueError("d25_probability_industry_labels_insufficient")
    if tuple(sorted(fitted_dates)) != request.split.fitting_dates or tuple(sorted(calibration_dates)) != request.split.calibration_dates:
        raise ValueError("d25_probability_dates_incomplete")
    weights = CalibrateD25Probability(np.concatenate(scores), np.concatenate(labels), check)
    return D25ProbabilityArtifact(
        request.profile, request.binding, request.contract.feature_manifest.names,
        tuple(sorted(fitted_dates)), tuple(sorted(calibration_dates)),
        weights, tuple(models),
    )


def _read_training_matrix(
    batches: Iterable[TrainingSampleBatch], check: Callable[[], None],
) -> tuple[TrainingSampleMatrix, frozenset[date]]:
    matrices: list[np.ndarray] = []
    labels: list[np.ndarray] = []
    dates: set[date] = set()
    for batch in batches:
        check()
        matrices.append(batch.features)
        labels.append(batch.labels)
        dates.update(batch.trade_dates)
    features = np.concatenate(matrices) if matrices else np.empty((0, 0))
    targets = np.concatenate(labels) if labels else np.empty(0)
    return TrainingSampleMatrix(features, targets), frozenset(dates)


def _fit_industry(
    training: TrainingSampleMatrix, parameters: ModelFittingParameters, threads: int, check: Callable[[], None],
) -> tuple[D25Model, Callable[[np.ndarray], np.ndarray]]:
    if isinstance(parameters, LightGBMFittingParameters):
        return FitD25Lightgbm(training, parameters, threads, check)
    if isinstance(parameters, TcnFittingParameters):
        return FitD25Tcn(training, parameters, threads, check)
    return FitD25Logistic(training, parameters, check)
