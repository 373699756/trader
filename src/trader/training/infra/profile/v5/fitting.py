"""Bounded batch fitting, train-only transforms and independent calibration."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import torch

from trader.training.infra.model_bundles.bundle_codec import TrainedTcnModelSnapshot
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_evaluation import CalibrationAccumulator
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.profile.v5.network import TcnNetwork
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingIndustryCounts, TrainingSplitName


def FitTcnIndustry(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    contract: TrainedHeadContract,
    parameters: TcnFittingParameters,
    heartbeat: Callable[[], None],
) -> FittedIndustryModel:
    width = len(contract.feature_positions)
    auxiliary = width - 360
    torch.manual_seed(parameters.seed)
    network = TcnNetwork(auxiliary, parameters.dropout)
    sums = np.zeros(auxiliary + 6)
    squares = np.zeros_like(sums)
    rows = 0
    for batch in samples.iter_batches(counts.industry, contract, "training", batch_size=parameters.batch_size):
        heartbeat()
        values = batch.features
        seq = values[:, auxiliary:].reshape(-1, 6)
        sums[:auxiliary] += values[:, :auxiliary].sum(axis=0)
        squares[:auxiliary] += (values[:, :auxiliary] ** 2).sum(axis=0)
        sums[auxiliary:] += seq.sum(axis=0)
        squares[auxiliary:] += (seq**2).sum(axis=0)
        rows += len(values)
    divisors = np.asarray([rows] * auxiliary + [rows * 60] * 6)
    means = sums / divisors
    scales = np.sqrt(np.maximum(0, squares / divisors - means**2))
    scales = np.where(scales > 1e-12, scales, 1.0)
    means = np.concatenate((means[:auxiliary], np.tile(means[auxiliary:], 60)))
    scales = np.concatenate((scales[:auxiliary], np.tile(scales[auxiliary:], 60)))
    optimizer = torch.optim.AdamW(
        network.parameters(), lr=parameters.learning_rate, weight_decay=parameters.weight_decay
    )
    best = math.inf
    stale = 0
    best_weights = network.tensors()

    def evaluate(split: TrainingSplitName) -> tuple[float, int]:
        error, count = 0.0, 0
        for batch in samples.iter_batches(counts.industry, contract, split, batch_size=parameters.batch_size):
            heartbeat()
            predicted = network.predict((batch.features - means) / scales)
            error += float(np.square(predicted - batch.labels).sum())
            count += len(predicted)
        return error, count

    for _epoch in range(parameters.epochs):
        network.train()
        for batch in samples.iter_batches(counts.industry, contract, "training", batch_size=parameters.batch_size):
            heartbeat()
            optimizer.zero_grad(set_to_none=True)
            predicted = network(torch.from_numpy((batch.features - means) / scales))
            loss = torch.square(predicted - torch.from_numpy(batch.labels.copy())).mean()
            if not torch.isfinite(loss):
                raise ValueError("V5 nonfinite training loss")
            loss.backward()
            optimizer.step()
        error, count = evaluate("early_stopping")
        metric = error / count
        if not math.isfinite(metric):
            raise ValueError("V5 nonfinite early stopping loss")
        if metric < best:
            best, stale, best_weights = metric, 0, network.tensors()
        else:
            stale += 1
            if stale >= parameters.early_stopping_rounds:
                break
    network.load_tensors(best_weights)
    # Only centered sufficient statistics are retained, never the complete calibration matrix.
    calibration = CalibrationAccumulator()
    for batch in samples.iter_batches(counts.industry, contract, "calibration", batch_size=parameters.batch_size):
        heartbeat()
        predicted = network.predict((batch.features - means) / scales)
        calibration.add(predicted, batch.labels, batch.trade_dates)
    calibrated = calibration.finish()
    return FittedIndustryModel(
        counts.industry,
        TrainedTcnModelSnapshot(
            tuple(float(value) for value in means),
            tuple(float(value) for value in scales),
            calibrated.intercept,
            calibrated.slope,
            parameters.dropout,
            best_weights,
        ),
        counts.training,
        calibrated,
    )


__all__ = ["FitTcnIndustry"]
