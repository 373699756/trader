"""Fit threshold-specific D25 models and calibrate fixed weights out of sample."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import lightgbm as lgb
import numpy as np

from trader.recommendation.domain.market.v1.features import LEARNED_RANK_FEATURE_IDS
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.production_diagnostic_dates import ProductionDiagnosticDatePlan
from trader.training.domain.v1.learned_rank import LearnedRankDateSplit, V1TrainingSettings
from trader.training.infra.d25_probability import (
    D25LogisticModel,
    D25Model,
    D25ProbabilityArtifact,
    D25ProbabilityBinding,
    D25TcnModel,
    D25TreeModel,
)
from trader.training.infra.logistic_fitting import FitBinaryLogistic
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_fitting import LightGBMFittingParameters, ModelFittingParameters, RidgeFittingParameters
from trader.training.infra.profile.v1.fitting import FitD25LearnedRankClassifier
from trader.training.infra.profile.v1.samples import (
    CheckV1Cancellation,
    LearnedRankMatrixRequest,
    LearnedRankSampleIndex,
)
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.profile.v5.binary_fitting import FitBinaryTcn
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


@dataclass(frozen=True, slots=True)
class _FittedProbability:
    model: D25Model
    logits: Callable[[np.ndarray], np.ndarray]


def FitV1D25Probability(
    samples: LearnedRankSampleIndex,
    directory: Path,
    split: LearnedRankDateSplit,
    settings: V1TrainingSettings,
    binding: D25ProbabilityBinding,
    *,
    compute_threads: int,
    cancel_requested: Callable[[], bool] | None,
) -> D25ProbabilityArtifact:
    with ExitStack() as resources:
        fit, early, calibration = samples.read_matrices(
            tuple(LearnedRankMatrixRequest(days, Strategy.D25, True) for days in (
                split.fitting, split.early_stopping, split.calibration,
            )), directory, resources, cancel_requested=cancel_requested,
        )
        if any(tuple(sorted(set(matrix.dates))) != dates for matrix, dates in (
            (fit, split.fitting), (early, split.early_stopping), (calibration, split.calibration),
        )):
            raise ValueError("d25_probability_v1_dates_incomplete")
        tree = FitD25LearnedRankClassifier(
            fit, early, settings, compute_threads=compute_threads, cancel_requested=cancel_requested,
        )
        scores = np.asarray(tree.predict(calibration.features, raw_score=True, num_threads=compute_threads))
        weights = _calibrate(scores, calibration.targets, lambda: CheckV1Cancellation(cancel_requested))
    return D25ProbabilityArtifact(
        "v1", binding, LEARNED_RANK_FEATURE_IDS,
        tuple(sorted(set((*fit.dates, *early.dates)))), tuple(sorted(set(calibration.dates))),
        weights, (("", D25TreeModel(tree.model_to_string())),),
    )


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
        if len(training.labels) < request.parameters.minimum_industry_training_rows or not _has_both_labels(training.labels):
            continue
        model = _fit_industry(training, request.parameters, request.compute_threads, check)
        for batch in samples.iter_batches(industry, request.contract, "calibration", d25_probability=True):
            check()
            scores.append(model.logits(batch.features))
            labels.append(batch.labels)
            calibration_dates.update(batch.trade_dates)
        models.append((industry, model.model))
        fitted_dates.update(dates)
    if not models or not scores:
        raise ValueError("d25_probability_industry_labels_insufficient")
    if tuple(sorted(fitted_dates)) != request.split.fitting_dates or tuple(sorted(calibration_dates)) != request.split.calibration_dates:
        raise ValueError("d25_probability_dates_incomplete")
    weights = _calibrate(np.concatenate(scores), np.concatenate(labels), check)
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
) -> _FittedProbability:
    if isinstance(parameters, LightGBMFittingParameters):
        return _fit_lightgbm(training, parameters, threads, check)
    if isinstance(parameters, TcnFittingParameters):
        return _fit_tcn(training, parameters, threads, check)
    return _fit_logistic(training, parameters, check)


def _fit_lightgbm(
    training: TrainingSampleMatrix, parameters: LightGBMFittingParameters, threads: int, check: Callable[[], None],
) -> _FittedProbability:
    def cancellation_callback(_environment: lgb.callback.CallbackEnv) -> None:
        check()

    tree = lgb.train(
        {"objective": "binary", "metric": "binary_logloss", "learning_rate": parameters.learning_rate,
         "max_depth": parameters.max_depth, "num_leaves": parameters.num_leaves,
         "min_data_in_leaf": parameters.minimum_leaf_rows, "max_bin": parameters.maximum_bins,
         "histogram_pool_size": parameters.histogram_pool_mib, "seed": parameters.seed,
         "deterministic": True, "force_col_wise": True, "num_threads": threads, "verbosity": -1},
        lgb.Dataset(training.features, label=training.labels),
        num_boost_round=parameters.boosting_rounds, callbacks=[cancellation_callback],
    )

    def predict(features: np.ndarray) -> np.ndarray:
        return np.asarray(tree.predict(features, raw_score=True, num_threads=threads))

    return _FittedProbability(D25TreeModel(tree.model_to_string()), predict)


def _fit_logistic(
    training: TrainingSampleMatrix, parameters: RidgeFittingParameters, check: Callable[[], None],
) -> _FittedProbability:
    means = training.features.mean(axis=0)
    scale = training.features.std(axis=0)
    scales = np.where(scale > 1e-12, scale, 1.0)
    intercept, weights = FitBinaryLogistic((training.features - means) / scales, training.labels, parameters.penalty, check)

    def predict(features: np.ndarray) -> np.ndarray:
        return np.asarray(intercept + (features - means) / scales @ weights)

    return _FittedProbability(D25LogisticModel(tuple(means), tuple(scales), intercept, tuple(weights)), predict)


def _fit_tcn(
    training: TrainingSampleMatrix, parameters: TcnFittingParameters, threads: int, check: Callable[[], None],
) -> _FittedProbability:
    if parameters.epochs != 10:
        raise ValueError("d25_probability_v5_requires_10_epochs")
    fitted = FitBinaryTcn(training, parameters, threads, check)
    return _FittedProbability(
        D25TcnModel(tuple(fitted.means), tuple(fitted.scales), parameters.dropout,
                    fitted.network.tensors(), parameters.architecture), fitted.logits,
    )


def _has_both_labels(labels: np.ndarray) -> bool:
    return bool(np.any(labels == 0) and np.any(labels == 1))


def _calibrate(scores: np.ndarray, labels: np.ndarray, check: Callable[[], None]) -> tuple[float, float]:
    if not _has_both_labels(labels):
        raise ValueError("d25_probability_calibration_labels_insufficient")
    intercept, slopes = FitBinaryLogistic(scores[:, None], labels, 0.001, check)
    slope = float(slopes[0])
    if not np.isfinite(slope) or slope <= 0:
        raise ValueError("d25_probability_calibration_not_monotone")
    return intercept, slope
