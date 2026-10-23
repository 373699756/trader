"""Bounded batch fitting, train-only transforms and independent calibration."""

from __future__ import annotations

import math
from collections.abc import Callable

import numpy as np
import torch

from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.profile.v5.network import TcnNetwork
from trader.training.infra.profile.v5.settings import TcnCalibrationError, TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingIndustryCounts, TrainingSplitName


def FitTcnIndustry(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    contract: TrainedHeadContract,
    parameters: TcnFittingParameters,
    heartbeat: Callable[[], None],
) -> dict[str, object]:
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
    best_epoch = 0

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
            best_epoch = _epoch + 1
        else:
            stale += 1
            if stale >= parameters.early_stopping_rounds:
                break
    network.load_tensors(best_weights)
    # Only sufficient statistics are retained, never the complete calibration matrix.
    n = sx = sy = sxx = sxy = 0.0
    for batch in samples.iter_batches(counts.industry, contract, "calibration", batch_size=parameters.batch_size):
        heartbeat()
        predicted = network.predict((batch.features - means) / scales)
        n += len(predicted)
        sx += float(predicted.sum())
        sy += float(batch.labels.sum())
        sxx += float(predicted @ predicted)
        sxy += float(predicted @ batch.labels)
    variance = sxx - sx * sx / n
    if n < 2 or variance <= 1e-20:
        raise TcnCalibrationError("V5 calibration predictions are degenerate")
    slope = (sxy - sx * sy / n) / variance
    intercept = (sy - slope * sx) / n
    error, absolute_error, evaluated = 0.0, 0.0, 0
    for batch in samples.iter_batches(counts.industry, contract, "validation", batch_size=parameters.batch_size):
        heartbeat()
        predicted = intercept + slope * network.predict((batch.features - means) / scales)
        residual = predicted - batch.labels
        error += float(np.square(residual).sum())
        absolute_error += float(np.abs(residual).sum())
        evaluated += len(predicted)
    if evaluated != counts.validation or not math.isfinite(error):
        raise ValueError("V5 validation partition is invalid")
    return {
        "transformer_means": means.tolist(),
        "transformer_scales": scales.tolist(),
        "calibration_intercept": intercept,
        "calibration_slope": slope,
        "prediction_metrics": {
            "rows": evaluated,
            "mae": absolute_error / evaluated,
            "rmse": math.sqrt(error / evaluated),
            "best_epoch": best_epoch,
            "early_stopping_mse": best,
        },
        "tcn_network": {
            "contract": SEQUENCE_CONTRACT,
            "dropout": parameters.dropout,
            "weights": [{"name": t.name, "shape": list(t.shape), "values": list(t.values)} for t in best_weights],
        },
        "training_rows": counts.training,
        "validation_rows": counts.validation,
    }


__all__ = ["FitTcnIndustry"]
