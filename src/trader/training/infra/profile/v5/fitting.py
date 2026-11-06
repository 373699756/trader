"""Bounded batch fitting, train-only transforms and independent calibration."""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, replace

import numpy as np
import torch

from trader.training.application.tomorrow_training import TrainingFitDetail, TrainingFitPhase
from trader.training.domain.v5.early_stopping import TcnEarlyStopping
from trader.training.domain.final_fit import FinalFitDatePlan
from trader.training.infra.model_bundles.bundle_codec import TrainedTcnModelSnapshot
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.model_evaluation import CalibrationAccumulator
from trader.training.infra.model_fitting_results import FittedIndustryModel
from trader.training.infra.profile.v5.batch_cache import CacheTcnBatches, PrepareTcnBatches, TcnBatchCache, TcnPreparedBatches
from trader.training.infra.profile.v5.network import TcnNetwork
from trader.training.infra.profile.v5.settings import TcnFittingParameters
from trader.training.infra.sample_index import FinalFitIndustryCounts, SQLiteTrainingSampleIndex, TrainingIndustryCounts, TrainingSampleMatrix


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
    with ExitStack() as resources:
        raw_resources = resources.enter_context(ExitStack())
        raw = raw_resources.enter_context(CacheTcnBatches(samples, counts, contract, parameters.batch_size, heartbeat))
        auxiliary = len(contract.feature_positions) - 360
        torch.manual_seed(parameters.seed)
        network = TcnNetwork(auxiliary, parameters.dropout, parameters.architecture)
        progress.publish("normalization")
        means, scales = _transforms(raw, auxiliary, heartbeat)
        cache: TcnBatchCache | TcnPreparedBatches = raw
        if samples.execution_options.normalized_tcn_batches:
            cache = resources.enter_context(PrepareTcnBatches(
                raw,
                means,
                scales,
                workspace=samples.workspace,
                memory_bytes=samples.sample_settings.tcn_batch_memory_bytes,
                compute_dtype=parameters.compute_dtype,
                heartbeat=heartbeat,
            ))
            raw_resources.close()
            del raw
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
                parameters.architecture,
            ),
            counts.training,
            calibrated,
        )


def RefitTcnIndustry(
    samples: SQLiteTrainingSampleIndex,
    counts: FinalFitIndustryCounts,
    contract: TrainedHeadContract,
    plan: FinalFitDatePlan,
    parameters: TcnFittingParameters,
    original: FittedIndustryModel,
    heartbeat: Callable[[], None],
    *,
    report: Callable[[TrainingFitDetail], None] | None = None,
) -> FittedIndustryModel:
    """Use configured epochs on the final mature window with a frozen diagnostic calibrator."""
    auxiliary = len(contract.feature_positions) - 360
    progress = _TcnProgress(
        TrainingFitDetail(counts.industry, counts.rows, "preparation", total_epochs=parameters.epochs), report,
    )
    # The sample index owns the bounded memory/mapped matrix and its cleanup.
    with samples.final_fit_data(counts, contract, plan) as data:
        progress.publish("normalization")
        means, scales = _final_transforms(data, auxiliary, parameters.batch_size, heartbeat)
        torch.manual_seed(parameters.seed)
        network = TcnNetwork(auxiliary, parameters.dropout, parameters.architecture)
        optimizer = torch.optim.AdamW(
            network.parameters(), lr=parameters.learning_rate, weight_decay=parameters.weight_decay,
        )
        batches = math.ceil(counts.rows / parameters.batch_size)
        for epoch in range(1, parameters.epochs + 1):
            network.train()
            progress.publish("training", epoch, 0, batches)
            for number, start in enumerate(range(0, counts.rows, parameters.batch_size), start=1):
                heartbeat()
                end = start + parameters.batch_size
                optimizer.zero_grad(set_to_none=True)
                features = torch.from_numpy((data.features[start:end] - means) / scales).to(network.compute_dtype)
                labels = torch.from_numpy(data.labels[start:end].copy()).to(network.compute_dtype)
                loss = torch.square(network(features) - labels).mean()
                if not torch.isfinite(loss):
                    raise ValueError("V5 nonfinite final refit loss")
                loss.backward()
                optimizer.step()
                progress.publish("training", epoch, number, batches)
        return FittedIndustryModel(
            counts.industry,
            TrainedTcnModelSnapshot(
                tuple(float(value) for value in means), tuple(float(value) for value in scales),
                original.calibration.intercept, original.calibration.slope, parameters.dropout,
                network.tensors(), parameters.architecture,
            ),
            counts.rows, original.calibration,
        )


def _final_transforms(
    data: TrainingSampleMatrix, auxiliary: int, batch_size: int, heartbeat: Callable[[], None],
) -> tuple[np.ndarray, np.ndarray]:
    sums = np.zeros(auxiliary + 6)
    squares = np.zeros_like(sums)
    for start in range(0, len(data.labels), batch_size):
        heartbeat()
        values = data.features[start:start + batch_size]
        sequence = values[:, auxiliary:].reshape(-1, 6)
        sums[:auxiliary] += values[:, :auxiliary].sum(axis=0)
        squares[:auxiliary] += (values[:, :auxiliary] ** 2).sum(axis=0)
        sums[auxiliary:] += sequence.sum(axis=0)
        squares[auxiliary:] += (sequence ** 2).sum(axis=0)
    divisors = np.asarray([len(data.labels)] * auxiliary + [len(data.labels) * 60] * 6)
    means = sums / divisors
    scales = np.sqrt(np.maximum(0, squares / divisors - means ** 2))
    scales = np.where(scales > 1e-12, scales, 1.0)
    return (
        np.concatenate((means[:auxiliary], np.tile(means[auxiliary:], 60))),
        np.concatenate((scales[:auxiliary], np.tile(scales[auxiliary:], 60))),
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
    cache: TcnBatchCache | TcnPreparedBatches
    heartbeat: Callable[[], None]
    means: np.ndarray
    scales: np.ndarray
    progress: _TcnProgress

    def _training_batches(self) -> Iterator[TrainingSampleMatrix | tuple[torch.Tensor, torch.Tensor]]:
        if isinstance(self.cache, TcnPreparedBatches):
            yield from self.cache.training_batches()
        else:
            yield from self.cache.batches("training")

    def _early_stopping_predictions(self) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        if isinstance(self.cache, TcnPreparedBatches):
            for features, labels in self.cache.early_stopping_batches():
                self.heartbeat()
                self.network.eval()
                with torch.inference_mode():
                    predicted = self.network(features)
                    values = np.asarray(predicted.numpy(), dtype=np.float64)
                yield values, labels
        else:
            for batch in self.cache.batches("early_stopping"):
                self.heartbeat()
                yield self.network.predict((batch.features - self.means) / self.scales), batch.labels

    def _evaluate(self, epoch: int) -> float:
        error, count = 0.0, 0
        batches = math.ceil(len(self.cache.early_stopping) / self.cache.batch_size)
        self.progress.publish("early_stopping", epoch, 0, batches)
        for number, (predicted, labels) in enumerate(self._early_stopping_predictions(), start=1):
            error += float(np.square(predicted - labels).sum())
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
        stopping = TcnEarlyStopping(parameters.early_stopping_relative_delta)
        best_weights = network.tensors()
        batches = math.ceil(len(self.cache.training) / self.cache.batch_size)
        for epoch in range(1, parameters.epochs + 1):
            network.train()
            self.progress.publish("training", epoch, 0, batches)
            for number, batch in enumerate(self._training_batches(), start=1):
                self.heartbeat()
                optimizer.zero_grad(set_to_none=True)
                if isinstance(batch, TrainingSampleMatrix):
                    predicted = network(
                        torch.from_numpy((batch.features - self.means) / self.scales).to(network.compute_dtype)
                    )
                    labels = torch.from_numpy(batch.labels.copy()).to(network.compute_dtype)
                else:
                    predicted = network(batch[0])
                    labels = batch[1]
                loss = torch.square(predicted - labels).mean()
                if not torch.isfinite(loss):
                    raise ValueError("V5 nonfinite training loss")
                loss.backward()
                optimizer.step()
                self.progress.publish("training", epoch, number, batches)
            metric = self._evaluate(epoch)
            if stopping.observe(metric):
                best_weights = network.tensors()
            if stopping.stale >= parameters.early_stopping_rounds:
                break
        network.load_tensors(best_weights)
        return epoch


__all__ = ["FitTcnIndustry", "RefitTcnIndustry"]
