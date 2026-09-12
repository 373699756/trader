"""Bounded industry-by-industry fitting for Tomorrow V3."""

from __future__ import annotations

from typing import Literal

import lightgbm as lgb
import numpy as np

from trader.application.research.tomorrow_training import (
    TOMORROW_TRAINING_COMPUTE_THREADS,
    TomorrowTrainingProgress,
    TomorrowTrainingProgressPort,
)
from trader.domain.research.baostock_daily import BaoStockTrainingSplit
from trader.infra.scoring.profiles.v3.training_sample_repository import (
    SQLiteTomorrowTrainingSampleRepository,
)


def fit_industry_models(
    samples: SQLiteTomorrowTrainingSampleRepository,
    split: BaoStockTrainingSplit,
    *,
    progress: TomorrowTrainingProgressPort | None = None,
) -> tuple[dict[str, dict[str, object]], int, int]:
    samples.require_split(split)
    models: dict[str, dict[str, object]] = {}
    workloads = samples.industry_counts()
    _publish(progress, "started", 0, len(workloads))
    for position, counts in enumerate(workloads, start=1):
        if counts.training < 20_000 or counts.calibration == 0 or counts.early_stopping == 0 or counts.validation == 0:
            _publish(
                progress,
                "completed" if position == len(workloads) else "running",
                position,
                len(workloads),
                len(models),
            )
            continue
        industry = counts.industry
        data = samples.industry_data(counts)
        train = data.training
        early = data.early_stopping
        calibration = data.calibration
        del data
        training_count = counts.training
        means = train.features.mean(axis=0)
        standard_deviations = train.features.std(axis=0)
        scales = np.where(standard_deviations > 1e-12, standard_deviations, 1.0)
        normalized = np.empty_like(train.features)
        np.subtract(train.features, means, out=normalized)
        np.divide(normalized, scales, out=normalized)
        training_labels = train.labels
        del train
        penalty = np.eye(7, dtype=np.float64) * 10.0
        penalty[0, 0] = 0.0
        gram = np.empty((7, 7), dtype=np.float64)
        feature_sums = normalized.sum(axis=0)
        gram[0, 0] = len(normalized)
        gram[0, 1:] = feature_sums
        gram[1:, 0] = feature_sums
        gram[1:, 1:] = normalized.T @ normalized
        target = np.empty(7, dtype=np.float64)
        target[0] = training_labels.sum()
        target[1:] = normalized.T @ training_labels
        coefficients = np.linalg.solve(gram + penalty, target)
        del feature_sums, gram, penalty, target

        early_features = (early.features - means) / scales
        booster = lgb.train(
            {
                "objective": "regression_l2",
                "learning_rate": 0.05,
                "max_depth": 3,
                "num_leaves": 7,
                "min_data_in_leaf": 20,
                "num_boost_round": 200,
                "max_bin": 63,
                "deterministic": True,
                "seed": 0,
                "feature_fraction_seed": 0,
                "bagging_seed": 0,
                "data_random_seed": 0,
                "num_threads": TOMORROW_TRAINING_COMPUTE_THREADS,
                "force_col_wise": True,
                "histogram_pool_size": 64,
                "verbosity": -1,
            },
            lgb.Dataset(normalized, label=training_labels),
            num_boost_round=200,
            valid_sets=[lgb.Dataset(early_features, label=early.labels)],
            callbacks=[lgb.early_stopping(20, verbose=False)],
        )
        booster.free_dataset()
        del early, early_features

        calibration_features = (calibration.features - means) / scales
        tree = booster.predict(calibration_features, num_iteration=booster.best_iteration)
        ridge = coefficients[0] + calibration_features @ coefficients[1:]
        predicted = 0.5 * ridge + 0.5 * tree
        slope, intercept = np.polyfit(predicted, calibration.labels, 1) if counts.calibration >= 2 else (1.0, 0.0)
        models[industry] = {
            "transformer_means": means.tolist(),
            "transformer_scales": scales.tolist(),
            "ridge_intercept": float(coefficients[0]),
            "ridge_coefficients": coefficients[1:].tolist(),
            "lightgbm_model": booster.model_to_string(num_iteration=booster.best_iteration),
            "lightgbm_best_iteration": int(booster.best_iteration),
            "calibration_intercept": float(intercept),
            "calibration_slope": float(slope),
            "training_rows": training_count,
            "validation_rows": counts.validation,
        }
        del (
            calibration,
            calibration_features,
            coefficients,
            normalized,
            predicted,
            ridge,
            training_labels,
            tree,
            booster,
        )
        _publish(
            progress, "completed" if position == len(workloads) else "running", position, len(workloads), len(models)
        )
    if not workloads:
        _publish(progress, "completed", 0, 0)
    return models, samples.split_count("training"), samples.split_count("validation")


def _publish(
    progress: TomorrowTrainingProgressPort | None,
    state: Literal["started", "running", "completed"],
    completed_units: int,
    total_units: int,
    produced_units: int = 0,
) -> None:
    if progress is not None:
        progress.publish(
            TomorrowTrainingProgress(
                "model_fit",
                state,
                completed_units,
                total_units,
                produced_units,
            )
        )


__all__ = ["fit_industry_models"]
