"""Fit the channel and auxiliary feature transforms shared by TCN models."""

from collections.abc import Callable, Iterable

import numpy as np

from trader.training.infra.sample_index import TrainingSampleMatrix


def FitTcnBatchTransforms(batches: Iterable[np.ndarray], auxiliary: int, heartbeat: Callable[[], None]) -> tuple[np.ndarray, np.ndarray]:
    sums = np.zeros(auxiliary + 6)
    squares = np.zeros_like(sums)
    rows = 0
    for values in batches:
        heartbeat()
        seq = values[:, auxiliary:].reshape(-1, 6)
        sums[:auxiliary] += values[:, :auxiliary].sum(axis=0)
        squares[:auxiliary] += (values[:, :auxiliary] ** 2).sum(axis=0)
        sums[auxiliary:] += seq.sum(axis=0)
        squares[auxiliary:] += (seq**2).sum(axis=0)
        rows += len(values)
    if rows == 0:
        raise ValueError("tcn_feature_transforms_require_rows")
    divisors = np.asarray([rows] * auxiliary + [rows * 60] * 6)
    means = sums / divisors
    scales = np.sqrt(np.maximum(0, squares / divisors - means**2))
    scales = np.where(scales > 1e-12, scales, 1.0)
    return (
        np.concatenate((means[:auxiliary], np.tile(means[auxiliary:], 60))),
        np.concatenate((scales[:auxiliary], np.tile(scales[auxiliary:], 60))),
    )


def FitTcnFeatureTransforms(
    data: TrainingSampleMatrix, auxiliary: int, batch_size: int, heartbeat: Callable[[], None],
) -> tuple[np.ndarray, np.ndarray]:
    batches = (data.features[start:start + batch_size] for start in range(0, len(data.labels), batch_size))
    return FitTcnBatchTransforms(batches, auxiliary, heartbeat)
