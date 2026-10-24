"""Bounded batch fitting, train-only transforms and independent calibration."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np
import torch

from trader.training.application.tomorrow_training import TrainingFitDetail, TrainingFitPhase
from trader.training.infra.model_bundles.bundle_codec import TrainedTcnModelSnapshot
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_evaluation import CalibrationAccumulator
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.profile.v5.batch_cache import CacheTcnBatches, TcnBatchCache
from trader.training.infra.profile.v5.network import TcnNetwork
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex, TrainingIndustryCounts


def FitTcnIndustry(
    samples: SQLiteTrainingSampleIndex,
    counts: TrainingIndustryCounts,
    contract: TrainedHeadContract,
    parameters: TcnFittingParameters,
    heartbeat: Callable[[], None],
    *,
    report: Callable[[TrainingFitDetail], None] | None = None,
) -> FittedIndustryModel:
    progress = _TcnProgress(
        TrainingFitDetail(counts.industry, counts.training, "batch_cache", total_epochs=parameters.epochs), report
    )
    progress.publish("batch_cache")
    with CacheTcnBatches(samples, counts, contract, parameters.batch_size, heartbeat) as cache:
        auxiliary = len(contract.feature_positions) - 360
        torch.manual_seed(parameters.seed)
        network = TcnNetwork(auxiliary, parameters.dropout)
        progress.publish("normalization")
        means, scales = _transforms(cache, auxiliary, heartbeat)
        epoch = _TcnFitState(network, parameters, cache, heartbeat, means, scales, progress).train()
        calibration = CalibrationAccumulator()
        batches = math.ceil(counts.calibration / parameters.batch_size)
        progress.publish("calibration", epoch, 0, batches)
        for number, batch in enumerate(
            samples.iter_batches(counts.industry, contract, "calibration", batch_size=parameters.batch_size), start=1
        ):
            heartbeat()
            predicted = network.predict((batch.features - means) / scales)
            calibration.add(predicted, batch.labels, batch.trade_dates)
            progress.publish("calibration", epoch, number, batches)
        calibrated = calibration.finish()
        return FittedIndustryModel(
            counts.industry,
            TrainedTcnModelSnapshot(
                tuple(float(value) for value in means),
                tuple(float(value) for value in scales),
                calibrated.intercept,
                calibrated.slope,
                parameters.dropout,
                network.tensors(),
            ),
            counts.training,
            calibrated,
        )


@dataclass(frozen=True)
class _TcnProgress:
    detail: TrainingFitDetail
    report: Callable[[TrainingFitDetail], None] | None

    def publish(self, phase: TrainingFitPhase, epoch: int = 0, completed: int = 0, total: int = 0) -> None:
        if self.report is not None:
            self.report(
                replace(self.detail, phase=phase, epoch=epoch, completed_batches=completed, total_batches=total)
            )


def _transforms(cache: TcnBatchCache, auxiliary: int, heartbeat: Callable[[], None]) -> tuple[np.ndarray, np.ndarray]:
    sums = np.zeros(auxiliary + 6)
    squares = np.zeros_like(sums)
    rows = 0
    for batch in cache.batches("training"):
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
    return (
        np.concatenate((means[:auxiliary], np.tile(means[auxiliary:], 60))),
        np.concatenate((scales[:auxiliary], np.tile(scales[auxiliary:], 60))),
    )


@dataclass(frozen=True)
class _TcnFitState:
    network: TcnNetwork
    parameters: TcnFittingParameters
    cache: TcnBatchCache
    heartbeat: Callable[[], None]
    means: np.ndarray
    scales: np.ndarray
    progress: _TcnProgress

    def _evaluate(self, epoch: int) -> float:
        error, count = 0.0, 0
        batches = math.ceil(len(self.cache.early_stopping) / self.cache.batch_size)
        self.progress.publish("early_stopping", epoch, 0, batches)
        for number, batch in enumerate(self.cache.batches("early_stopping"), start=1):
            self.heartbeat()
            predicted = self.network.predict((batch.features - self.means) / self.scales)
            error += float(np.square(predicted - batch.labels).sum())
            count += len(predicted)
            self.progress.publish("early_stopping", epoch, number, batches)
        metric = error / count
        if not math.isfinite(metric):
            raise ValueError("V5 nonfinite early stopping loss")
        return metric

    def train(self) -> int:
        network, parameters = self.network, self.parameters
        optimizer = torch.optim.AdamW(
            network.parameters(), lr=parameters.learning_rate, weight_decay=parameters.weight_decay
        )
        best, stale, best_weights = math.inf, 0, network.tensors()
        batches = math.ceil(len(self.cache.training) / self.cache.batch_size)
        for epoch in range(1, parameters.epochs + 1):
            network.train()
            self.progress.publish("training", epoch, 0, batches)
            for number, batch in enumerate(self.cache.batches("training"), start=1):
                self.heartbeat()
                optimizer.zero_grad(set_to_none=True)
                predicted = network(torch.from_numpy((batch.features - self.means) / self.scales))
                loss = torch.square(predicted - torch.from_numpy(batch.labels.copy())).mean()
                if not torch.isfinite(loss):
                    raise ValueError("V5 nonfinite training loss")
                loss.backward()
                optimizer.step()
                self.progress.publish("training", epoch, number, batches)
            metric = self._evaluate(epoch)
            if metric < best:
                best, stale, best_weights = metric, 0, network.tensors()
            else:
                stale += 1
                if stale >= parameters.early_stopping_rounds:
                    break
        network.load_tensors(best_weights)
        return epoch


__all__ = ["FitTcnIndustry"]
