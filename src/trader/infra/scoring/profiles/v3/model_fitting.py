"""Bounded industry-by-industry fitting shared by the three V3 heads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import lightgbm as lgb
import numpy as np

from trader.application.research.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
)
from trader.domain.research.baostock_daily import BaoStockTrainingSplit
from trader.infra.scoring.head_bundles.contracts import TrainedHeadContract
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteV3TrainingSampleRepository,
    V3TrainingIndustryCounts,
)


@dataclass(frozen=True)
class V3ModelFittingParameters:
    minimum_industry_training_rows: int = 20_000
    ridge_penalty: float = 10.0
    learning_rate: float = 0.05
    max_depth: int = 3
    num_leaves: int = 7
    minimum_leaf_rows: int = 20
    boosting_rounds: int = 200
    early_stopping_rounds: int = 20
    maximum_bins: int = 63
    histogram_pool_mib: int = 64
    seed: int = 0


V3_MODEL_FITTING_PARAMETERS = V3ModelFittingParameters()


@dataclass(frozen=True)
class _FitProgress:
    state: Literal["started", "running", "completed"]
    completed_units: int
    total_units: int
    produced_units: int = 0


def fit_industry_models(
    samples: SQLiteV3TrainingSampleRepository,
    split: BaoStockTrainingSplit,
    contract: TrainedHeadContract,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> tuple[dict[str, dict[str, object]], int, int]:
    samples.require_split(split)
    models: dict[str, dict[str, object]] = {}
    workloads = samples.industry_counts(contract)
    _publish(progress, contract, _FitProgress("started", 0, len(workloads)))
    parameters = V3_MODEL_FITTING_PARAMETERS
    for position, counts in enumerate(workloads, start=1):
        if (
            counts.training < parameters.minimum_industry_training_rows
            or counts.calibration == 0
            or counts.early_stopping == 0
            or counts.validation == 0
        ):
            _publish(progress, contract, _fit_progress(position, len(workloads), len(models)))
            continue
        models[counts.industry] = _fit_industry(samples, counts, contract, parameters)
        _publish(progress, contract, _fit_progress(position, len(workloads), len(models)))
    if not workloads:
        _publish(progress, contract, _FitProgress("completed", 0, 0))
    return models, samples.split_count("training", contract), samples.split_count("validation", contract)


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
    samples: SQLiteV3TrainingSampleRepository,
    counts: V3TrainingIndustryCounts,
    contract: TrainedHeadContract,
    parameters: V3ModelFittingParameters,
) -> dict[str, object]:
    data = samples.industry_data(counts, contract)
    train = data.training
    means = train.features.mean(axis=0)
    deviations = train.features.std(axis=0)
    scales = np.where(deviations > 1e-12, deviations, 1.0)
    normalized = (train.features - means) / scales
    coefficients = _ridge_coefficients(normalized, train.labels, parameters.ridge_penalty)
    early_features = (data.early_stopping.features - means) / scales
    booster = _fit_lightgbm(normalized, train.labels, early_features, data.early_stopping.labels, parameters)
    calibration_features = (data.calibration.features - means) / scales
    tree = booster.predict(calibration_features, num_iteration=booster.best_iteration)
    ridge = coefficients[0] + calibration_features @ coefficients[1:]
    predicted = 0.5 * ridge + 0.5 * tree
    slope, intercept = np.polyfit(predicted, data.calibration.labels, 1) if counts.calibration >= 2 else (1.0, 0.0)
    result = {
        "transformer_means": means.tolist(),
        "transformer_scales": scales.tolist(),
        "ridge_intercept": float(coefficients[0]),
        "ridge_coefficients": coefficients[1:].tolist(),
        "lightgbm_model": booster.model_to_string(num_iteration=booster.best_iteration),
        "lightgbm_best_iteration": int(booster.best_iteration),
        "calibration_intercept": float(intercept),
        "calibration_slope": float(slope),
        "training_rows": counts.training,
        "validation_rows": counts.validation,
    }
    return result


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
    training_features: np.ndarray,
    training_labels: np.ndarray,
    early_features: np.ndarray,
    early_labels: np.ndarray,
    parameters: V3ModelFittingParameters,
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
        lgb.Dataset(training_features, label=training_labels),
        num_boost_round=parameters.boosting_rounds,
        valid_sets=[lgb.Dataset(early_features, label=early_labels)],
        callbacks=[lgb.early_stopping(parameters.early_stopping_rounds, verbose=False)],
    )
    booster.free_dataset()
    return booster


__all__ = ["V3_MODEL_FITTING_PARAMETERS", "V3ModelFittingParameters", "fit_industry_models"]
