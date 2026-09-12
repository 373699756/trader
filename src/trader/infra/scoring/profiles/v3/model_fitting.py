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
    models: dict[str, dict[str, object]] = {}
    training_dates = frozenset(split.model_fit_dates)
    calibration_dates = frozenset(split.calibration_dates)
    early_dates = frozenset(split.early_stopping_dates)
    validation_dates = frozenset((*split.confirmation_dates, *split.daily_proxy_holdout_dates))
    industries = samples.industries(training_dates)
    _publish(progress, "started", 0, len(industries))
    for position, industry in enumerate(industries, start=1):
        train = samples.matrix_for(industry, training_dates)
        calibration_count = samples.count_for(industry, calibration_dates)
        early_count = samples.count_for(industry, early_dates)
        validation_count = samples.count_for(industry, validation_dates)
        training_count = len(train.labels)
        if training_count < 20_000 or calibration_count == 0 or early_count == 0 or validation_count == 0:
            _publish(
                progress,
                "completed" if position == len(industries) else "running",
                position,
                len(industries),
                len(models),
            )
            continue
        means = train.features.mean(axis=0)
        standard_deviations = train.features.std(axis=0)
        scales = np.where(standard_deviations > 1e-12, standard_deviations, 1.0)
        normalized = (train.features - means) / scales
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

        early = samples.matrix_for(industry, early_dates)
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

        calibration = samples.matrix_for(industry, calibration_dates)
        calibration_features = (calibration.features - means) / scales
        tree = booster.predict(calibration_features, num_iteration=booster.best_iteration)
        ridge = coefficients[0] + calibration_features @ coefficients[1:]
        predicted = 0.5 * ridge + 0.5 * tree
        slope, intercept = np.polyfit(predicted, calibration.labels, 1) if calibration_count >= 2 else (1.0, 0.0)
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
            "validation_rows": validation_count,
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
            progress, "completed" if position == len(industries) else "running", position, len(industries), len(models)
        )
    if not industries:
        _publish(progress, "completed", 0, 0)
    return models, samples.count(training_dates), samples.count(validation_dates)


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
