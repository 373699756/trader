"""Shared cross-industry LambdaRank and independently calibrated gross-return Ridge."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import lightgbm as lgb
import numpy as np

from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.utility_scoring import PercentileRanks
from trader.training.domain.v1.learned_rank import (
    LearnedRankContractHash,
    LearnedRankDateSplit,
    LearnedRankHead,
    LearnedRankPredictionMetrics,
    LearnedRankRelevance,
    V1TrainingSettings,
)
from trader.training.infra.profile.v1.samples import CheckV1Cancellation, LearnedRankMatrix

_CHUNK_ROWS = 8192


@dataclass(frozen=True)
class LearnedRankPartitions:
    fitting: LearnedRankMatrix
    early_stopping: LearnedRankMatrix
    calibration: LearnedRankMatrix
    validation: LearnedRankMatrix


def FitLearnedRankHead(
    strategy: Strategy,
    split: LearnedRankDateSplit,
    partitions: LearnedRankPartitions,
    settings: V1TrainingSettings,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> LearnedRankHead:
    """Read validation only after fitting, early stopping and past calibration finish."""

    fitting, early, calibration, validation = (
        partitions.fitting,
        partitions.early_stopping,
        partitions.calibration,
        partitions.validation,
    )
    if len(fitting.targets) < settings.minimum_training_rows:
        raise ValueError("learned_rank_training_rows_insufficient")
    weights = _daily_weights(fitting)
    means, scales = _transform(fitting, weights)
    for matrix in (fitting, early, calibration, validation):
        for start, end in _chunks(len(matrix.targets)):
            CheckV1Cancellation(cancel_requested)
            matrix.features[start:end] -= means
            matrix.features[start:end] /= scales
    coefficients, intercept = _ridge(fitting, weights, settings.ridge_penalty, cancel_requested)
    del weights

    def cancellation_callback(_environment: lgb.callback.CallbackEnv) -> None:
        CheckV1Cancellation(cancel_requested)

    booster = lgb.train(
        {
            "objective": "lambdarank",
            "metric": "ndcg",
            "ndcg_eval_at": [6],
            "label_gain": [0, 1, 2, 3, 4],
            "lambdarank_truncation_level": 10,
            "lambdarank_norm": False,
            "learning_rate": settings.learning_rate,
            "max_depth": settings.max_depth,
            "num_leaves": settings.num_leaves,
            "min_data_in_leaf": settings.minimum_leaf_rows,
            "max_bin": settings.maximum_bins,
            "histogram_pool_size": settings.histogram_pool_mib,
            "seed": settings.seed,
            "deterministic": True,
            "force_col_wise": True,
            "num_threads": 2,
            "verbosity": -1,
        },
        lgb.Dataset(
            fitting.features,
            label=_relevance(fitting, settings),
            weight=_daily_weights(fitting),
            group=np.asarray(fitting.groups, dtype=np.int32),
        ),
        num_boost_round=settings.boosting_rounds,
        valid_sets=[
            lgb.Dataset(
                early.features,
                label=_relevance(early, settings),
                weight=_daily_weights(early),
                group=np.asarray(early.groups, dtype=np.int32),
            )
        ],
        callbacks=[lgb.early_stopping(settings.early_stopping_rounds, verbose=False), cancellation_callback],
    )
    CheckV1Cancellation(cancel_requested)
    calibration_predictions = intercept + calibration.features @ coefficients
    calibration_weights = _daily_weights(calibration)
    x_mean = float(np.average(calibration_predictions, weights=calibration_weights))
    y_mean = float(np.average(calibration.targets, weights=calibration_weights))
    centered = calibration_predictions - x_mean
    variance = float(np.average(centered * centered, weights=calibration_weights))
    covariance = float(np.average(centered * (calibration.targets - y_mean), weights=calibration_weights))
    slope = covariance / (variance + 1e-12)
    calibration_intercept = y_mean - slope * x_mean
    predicted = calibration_intercept + slope * (intercept + validation.features @ coefficients)
    rank_raw = np.asarray(booster.predict(validation.features, num_threads=2), dtype=np.float64)
    metrics = _metrics(validation, predicted, rank_raw)
    return LearnedRankHead(
        strategy,
        LearnedRankContractHash(settings),
        split,
        tuple(float(value) for value in means),
        tuple(float(value) for value in scales),
        tuple(float(value) for value in coefficients),
        intercept,
        calibration_intercept,
        slope,
        booster.model_to_string(),
        max(1, booster.best_iteration),
        len(fitting.targets),
        len(early.targets),
        len(calibration.targets),
        tuple(sorted(set(fitting.industries))),
        metrics,
    )


def _chunks(size: int) -> Iterator[tuple[int, int]]:
    for start in range(0, size, _CHUNK_ROWS):
        yield start, min(size, start + _CHUNK_ROWS)


def _daily_weights(matrix: LearnedRankMatrix) -> np.ndarray:
    # Each day has identical total weight; the overall mean row weight is one.
    return np.repeat(
        np.asarray([len(matrix.targets) / (len(matrix.groups) * size) for size in matrix.groups]), matrix.groups
    )


def _transform(matrix: LearnedRankMatrix, weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    total = float(np.sum(weights))
    means = np.zeros(matrix.features.shape[1], dtype=np.float64)
    for start, end in _chunks(len(matrix.targets)):
        means += weights[start:end] @ matrix.features[start:end] / total
    variance = np.zeros_like(means)
    for start, end in _chunks(len(matrix.targets)):
        centered = matrix.features[start:end] - means
        variance += weights[start:end] @ (centered * centered) / total
    scales = np.sqrt(variance)
    scales[scales < 1e-12] = 1.0
    return means, scales


def _ridge(
    matrix: LearnedRankMatrix, weights: np.ndarray, penalty: float, cancel_requested: Callable[[], bool] | None
) -> tuple[np.ndarray, float]:
    width = matrix.features.shape[1]
    gram = np.eye(width, dtype=np.float64) * penalty
    target = np.zeros(width, dtype=np.float64)
    intercept = float(np.average(matrix.targets, weights=weights))
    for start, end in _chunks(len(matrix.targets)):
        CheckV1Cancellation(cancel_requested)
        values = matrix.features[start:end]
        weighted = values * weights[start:end, None]
        gram += values.T @ weighted
        target += weighted.T @ (matrix.targets[start:end] - intercept)
    return np.linalg.solve(gram, target), intercept


def _relevance(matrix: LearnedRankMatrix, settings: V1TrainingSettings) -> np.ndarray:
    result = np.empty(len(matrix.targets), dtype=np.int32)
    start = 0
    for size in matrix.groups:
        result[start : start + size] = LearnedRankRelevance(
            tuple(float(value) - settings.ranking_round_trip_cost for value in matrix.targets[start : start + size])
        )
        start += size
    return result


def _metrics(matrix: LearnedRankMatrix, predicted: np.ndarray, rank_raw: np.ndarray) -> LearnedRankPredictionMetrics:
    if (
        predicted.shape != matrix.targets.shape
        or rank_raw.shape != matrix.targets.shape
        or not np.all(np.isfinite(predicted))
        or not np.all(np.isfinite(rank_raw))
    ):
        raise ValueError("learned_rank_validation_predictions_invalid")
    daily_ic: list[float] = []
    daily_ndcg: list[float] = []
    start = 0
    for size in matrix.groups:
        end = start + size
        target_ranks = np.asarray(PercentileRanks(tuple(float(value) for value in matrix.targets[start:end])))
        prediction_ranks = np.asarray(PercentileRanks(tuple(float(value) for value in rank_raw[start:end])))
        denominator = float(np.std(target_ranks) * np.std(prediction_ranks))
        daily_ic.append(
            float(np.mean((target_ranks - np.mean(target_ranks)) * (prediction_ranks - np.mean(prediction_ranks))))
            / denominator
            if denominator > 0
            else 0.0
        )
        relevance = LearnedRankRelevance(tuple(float(value) for value in matrix.targets[start:end]))
        order = sorted(
            range(size), key=lambda offset: (-float(rank_raw[start + offset]), matrix.codes[start + offset])
        )[:6]
        ideal = sorted(relevance, reverse=True)[:6]
        dcg = math.fsum(relevance[index] / math.log2(position + 2) for position, index in enumerate(order))
        idcg = math.fsum(value / math.log2(position + 2) for position, value in enumerate(ideal))
        daily_ndcg.append(dcg / idcg if idcg > 0 else 1.0)
        start = end
    weights = _daily_weights(matrix)
    error = predicted - matrix.targets
    return LearnedRankPredictionMetrics(
        len(matrix.targets),
        len(predicted),
        len(set(matrix.codes)),
        len(matrix.groups),
        len(set(matrix.industries)),
        float(np.average(np.abs(error), weights=weights)),
        math.sqrt(float(np.average(error * error, weights=weights))),
        math.fsum(daily_ic) / len(daily_ic),
        math.fsum(daily_ndcg) / len(daily_ndcg),
    )


__all__ = ["FitLearnedRankHead", "LearnedRankPartitions"]
