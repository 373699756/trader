"""Bounded industry-by-industry fitting shared by trained scoring profiles."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

import lightgbm as lgb
import numpy as np

from trader.training.application.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
)
from trader.training.domain.training_dates import TrainingDateSplit
from trader.training.infra.model_bundles.bundle_codec import (
    TrainedLightGBMModelSnapshot,
    TrainedRidgeModelSnapshot,
    TrainedTcnModelSnapshot,
)
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_bundles.predictor import PredictRidgeBatch
from trader.training.infra.model_evaluation import (
    CalibrationAccumulator,
    CalibrationUnavailableError,
    PredictionMetrics,
    PredictionMetricsAccumulator,
)
from trader.training.infra.model_fitting_results import (
    FittedIndustryModel,
    FittedModelSnapshot,
    IndustryFitFailure,
    IndustryFitResult,
)
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.sample_index import (
    SQLiteTrainingSampleIndex,
    TrainingIndustryCounts,
    TrainingSampleMatrix,
)

if TYPE_CHECKING:
    from trader.training.infra.profile.v5.network import TcnNetwork

_MINIMUM_CALIBRATION_SAMPLES = 2
_MINIMUM_FEATURE_DEVIATION = 1e-12


@dataclass(frozen=True)
class LightGBMFittingParameters:
    minimum_industry_training_rows: int
    learning_rate: float
    max_depth: int
    num_leaves: int
    minimum_leaf_rows: int
    boosting_rounds: int
    early_stopping_rounds: int
    maximum_bins: int
    histogram_pool_mib: int
    seed: int


@dataclass(frozen=True)
class RidgeFittingParameters:
    minimum_industry_training_rows: int
    penalty: float


ModelFittingParameters = LightGBMFittingParameters | RidgeFittingParameters | TcnFittingParameters


def FittingParameterDocument(parameters: ModelFittingParameters) -> dict[str, object]:
    """Project fitting identity at the persisted contract boundary."""
    return asdict(parameters)


@dataclass(frozen=True)
class _FitProgress:
    state: Literal["started", "running", "completed"]
    completed_units: int
    total_units: int
    produced_units: int = 0


def FitIndustryModels(
    samples: SQLiteTrainingSampleIndex,
    split: TrainingDateSplit,
    contract: TrainedHeadContract,
    parameters: ModelFittingParameters,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> IndustryFitResult:
    samples.require_split(split)
    models: list[FittedIndustryModel] = []
    failures: list[IndustryFitFailure] = []
    if progress is not None:
        progress.publish(TomorrowTrainingProgress("industry_statistics", "started", 0, 1, strategy=contract.strategy))
    workloads = samples.industry_counts(contract)
    if progress is not None:
        progress.publish(TomorrowTrainingProgress("industry_statistics", "completed", 1, 1, strategy=contract.strategy))
    _publish(progress, contract, _FitProgress("started", 0, len(workloads)))
    for position, counts in enumerate(workloads, start=1):
        model, failure = _attempt_industry_fit(
            samples,
            counts,
            contract,
            parameters,
            partial(_publish, progress, contract, _FitProgress("running", position - 1, len(workloads), len(models))),
        )
        if model is not None:
            models.append(model)
        if failure:
            failures.append(
                IndustryFitFailure(
                    counts.industry,
                    failure,
                    counts.training,
                    counts.calibration,
                    counts.calibration_dates,
                    counts.validation,
                )
            )
        _publish(
            progress,
            contract,
            _FitProgress("running", position, len(workloads), len(models))
            if models and position == len(workloads)
            else _fit_progress(position, len(workloads), len(models)),
        )
    if not workloads:
        _publish(progress, contract, _FitProgress("completed", 0, 0))
    heartbeat = partial(
        _publish, progress, contract, _FitProgress("running", len(workloads), len(workloads), len(models))
    )
    if models:
        metrics, industry_metrics = _evaluate_validation(samples, contract, tuple(models), heartbeat)
        expected = {item.industry: item.validation for item in workloads}
        if any(value.predicted_rows != expected[industry] for industry, value in industry_metrics):
            raise ValueError("validation_sample_coverage_changed")
        _publish(progress, contract, _FitProgress("completed", len(workloads), len(workloads), len(models)))
    else:
        metrics = PredictionMetrics(sum(item.validation for item in workloads), 0, 0, 0, 0, None, None, None, 0)
        industry_metrics = ()
    return IndustryFitResult(
        tuple(models),
        tuple(failures),
        metrics,
        industry_metrics,
        sum(item.training for item in workloads),
        sum(item.holdout for item in workloads),
    )


def _attempt_industry_fit(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    contract: TrainedHeadContract,
    parameters: ModelFittingParameters,
    heartbeat: Callable[[], None],
) -> tuple[FittedIndustryModel | None, str]:
    failure = _coverage_failure(counts, parameters)
    if failure:
        return None, failure
    try:
        if isinstance(parameters, TcnFittingParameters):
            from trader.training.infra.profile.v5.fitting import FitTcnIndustry

            return FitTcnIndustry(samples, counts, contract, parameters, heartbeat), ""
        heartbeat()
        fitted = _fit_industry(samples, counts, contract, parameters, heartbeat)
        heartbeat()
        return fitted, ""
    except CalibrationUnavailableError as exc:
        return None, exc.reason


def _coverage_failure(counts: TrainingIndustryCounts, parameters: ModelFittingParameters) -> str:
    if counts.training < parameters.minimum_industry_training_rows:
        return "industry_training_rows_insufficient"
    if counts.calibration < _MINIMUM_CALIBRATION_SAMPLES or counts.calibration_dates < _MINIMUM_CALIBRATION_SAMPLES:
        return "calibration_samples_or_dates_insufficient"
    if isinstance(parameters, (LightGBMFittingParameters, TcnFittingParameters)) and counts.early_stopping == 0:
        return "industry_early_stopping_rows_insufficient"
    if counts.validation == 0:
        return "industry_validation_rows_insufficient"
    return ""


def _publish(
    progress: TomorrowTrainingProgressPort | None,
    contract: TrainedHeadContract,
    update: _FitProgress,
) -> None:
    if progress is not None:
        progress.publish(
            TomorrowTrainingProgress(
                "model_fit",
                update.state,
                update.completed_units,
                update.total_units,
                update.produced_units,
                strategy=contract.strategy,
            )
        )


def _fit_progress(position: int, total: int, produced: int) -> _FitProgress:
    return _FitProgress("completed" if position == total else "running", position, total, produced)


def _fit_industry(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    contract: TrainedHeadContract,
    parameters: LightGBMFittingParameters | RidgeFittingParameters,
    heartbeat: Callable[[], None],
) -> FittedIndustryModel:
    data = samples.industry_data(counts, contract)
    train = data.training
    means = train.features.mean(axis=0)
    deviations = train.features.std(axis=0)
    scales = np.where(deviations > _MINIMUM_FEATURE_DEVIATION, deviations, 1.0)
    normalized = train.features
    normalized -= means
    normalized /= scales
    calibration_features = data.calibration.features
    calibration_features -= means
    calibration_features /= scales
    if isinstance(parameters, LightGBMFittingParameters):
        early_features = data.early_stopping.features
        early_features -= means
        early_features /= scales
        booster = _fit_lightgbm(train, data.early_stopping, parameters, heartbeat)
        predicted = np.asarray(
            booster.predict(calibration_features, num_iteration=booster.best_iteration, num_threads=2)
        )
    else:
        coefficients = _ridge_coefficients(normalized, train.labels, parameters.penalty)
        predicted = PredictRidgeBatch(calibration_features, coefficients[1:], intercept=float(coefficients[0]))
    calibration = CalibrationAccumulator()
    calibration.add(predicted, data.calibration.labels, data.calibration_dates)
    calibrated = calibration.finish()
    common = (
        tuple(float(value) for value in means),
        tuple(float(value) for value in scales),
        calibrated.intercept,
        calibrated.slope,
    )
    if isinstance(parameters, LightGBMFittingParameters):
        model: FittedModelSnapshot = TrainedLightGBMModelSnapshot(
            *common, booster.model_to_string(num_iteration=booster.best_iteration), int(booster.best_iteration)
        )
    else:
        model = TrainedRidgeModelSnapshot(
            *common, float(coefficients[0]), tuple(float(value) for value in coefficients[1:])
        )
    return FittedIndustryModel(counts.industry, model, counts.training, calibrated)


class _ValidationPredictor:
    def __init__(self, model: FittedModelSnapshot) -> None:
        self._model = model
        self._means = np.asarray(model.transformer_means)
        self._scales = np.asarray(model.transformer_scales)
        self._booster = (
            lgb.Booster(model_str=model.lightgbm_model) if isinstance(model, TrainedLightGBMModelSnapshot) else None
        )
        self._network: TcnNetwork | None = None
        if isinstance(model, TrainedTcnModelSnapshot):
            from trader.training.infra.profile.v5.network import TcnNetwork

            self._network = TcnNetwork(len(model.transformer_means) - 360, model.dropout)
            self._network.load_tensors(model.weights)
        self._coefficients = (
            np.asarray(model.ridge_coefficients) if isinstance(model, TrainedRidgeModelSnapshot) else None
        )

    def predict(self, features: np.ndarray) -> np.ndarray:
        values = (features - self._means) / self._scales
        model = self._model
        if isinstance(model, TrainedLightGBMModelSnapshot) and self._booster is not None:
            predicted = np.asarray(
                self._booster.predict(values, num_iteration=model.lightgbm_best_iteration, num_threads=2)
            )
        elif isinstance(model, TrainedRidgeModelSnapshot) and self._coefficients is not None:
            predicted = PredictRidgeBatch(values, self._coefficients, intercept=model.ridge_intercept)
        elif isinstance(model, TrainedTcnModelSnapshot) and self._network is not None:
            predicted = self._network.predict(values)
        else:
            raise ValueError("validation_model_type_invalid")
        return np.asarray(model.calibration_intercept + model.calibration_slope * predicted, dtype=np.float64)


def _evaluate_validation(
    samples: SQLiteTrainingSampleIndex,
    contract: TrainedHeadContract,
    models: tuple[FittedIndustryModel, ...],
    heartbeat: Callable[[], None],
) -> tuple[PredictionMetrics, tuple[tuple[str, PredictionMetrics], ...]]:
    predictors = {item.industry: _ValidationPredictor(item.model) for item in models}
    overall = PredictionMetricsAccumulator()
    industries = {item.industry: PredictionMetricsAccumulator() for item in models}
    for batch in samples.iter_validation_batches(contract):
        heartbeat()
        predictions: list[float | None] = [None] * len(batch.codes)
        for industry in sorted(set(batch.industries) & predictors.keys()):
            positions = [index for index, value in enumerate(batch.industries) if value == industry]
            predicted = predictors[industry].predict(batch.features[positions])
            if len(predicted) != len(positions):
                raise ValueError("validation_prediction_count_invalid")
            for index, value in zip(positions, predicted, strict=True):
                predictions[index] = float(value)
        for index, prediction in enumerate(predictions):
            day, code, industry = batch.trade_dates[index], batch.codes[index], batch.industries[index]
            target = float(batch.labels[index])
            overall.add(day, code, industry, target, prediction)
            if industry in industries:
                industries[industry].add(day, code, industry, target, prediction)
    return overall.finish(), tuple((industry, value.finish()) for industry, value in sorted(industries.items()))


def _ridge_coefficients(features: np.ndarray, labels: np.ndarray, penalty_value: float) -> np.ndarray:
    width = features.shape[1] + 1
    penalty = np.eye(width, dtype=np.float64) * penalty_value
    penalty[0, 0] = 0.0
    gram = np.empty((width, width), dtype=np.float64)
    sums = features.sum(axis=0)
    gram[0, 0] = len(features)
    gram[0, 1:] = sums
    gram[1:, 0] = sums
    gram[1:, 1:] = features.T @ features
    target = np.empty(width, dtype=np.float64)
    target[0] = labels.sum()
    target[1:] = features.T @ labels
    return np.linalg.solve(gram + penalty, target)


def _fit_lightgbm(
    training: TrainingSampleMatrix,
    early: TrainingSampleMatrix,
    parameters: LightGBMFittingParameters,
    heartbeat: Callable[[], None],
) -> lgb.Booster:
    booster = lgb.train(
        {
            "objective": "regression_l2",
            "learning_rate": parameters.learning_rate,
            "max_depth": parameters.max_depth,
            "num_leaves": parameters.num_leaves,
            "min_data_in_leaf": parameters.minimum_leaf_rows,
            "num_boost_round": parameters.boosting_rounds,
            "max_bin": parameters.maximum_bins,
            "deterministic": True,
            "seed": parameters.seed,
            "feature_fraction_seed": parameters.seed,
            "bagging_seed": parameters.seed,
            "data_random_seed": parameters.seed,
            "num_threads": TOMORROW_TRAINING_COMPUTE_THREADS,
            "force_col_wise": True,
            "histogram_pool_size": parameters.histogram_pool_mib,
            "verbosity": -1,
        },
        lgb.Dataset(training.features, label=training.labels),
        num_boost_round=parameters.boosting_rounds,
        valid_sets=[lgb.Dataset(early.features, label=early.labels)],
        callbacks=[lgb.early_stopping(parameters.early_stopping_rounds, verbose=False), lambda _env: heartbeat()],
    )
    booster.free_dataset()
    return booster


__all__ = [
    "LightGBMFittingParameters",
    "ModelFittingParameters",
    "FittingParameterDocument",
    "RidgeFittingParameters",
    "FitIndustryModels",
]
