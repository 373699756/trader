"""Fit a bounded ten-epoch binary TCN for a closing-price event."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from trader.recommendation.domain.market.v5.features import SEQUENCE_CHANNELS, SEQUENCE_STEPS
from trader.training.infra.tcn.settings import TcnFittingParameters
from trader.training.infra.tcn.transforms import FitTcnFeatureTransforms
from trader.training.infra.sample_index import TrainingSampleMatrix

if TYPE_CHECKING:
    from trader.training.infra.tcn.network import TcnNetwork


@dataclass(frozen=True, slots=True)
class FittedBinaryTcn:
    network: TcnNetwork
    means: np.ndarray
    scales: np.ndarray

    def logits(self, features: np.ndarray) -> np.ndarray:
        if len(features) == 0:
            return np.empty(0, dtype=np.float64)
        return np.concatenate(tuple(
            self.network.predict((features[start:start + 256] - self.means) / self.scales)
            for start in range(0, len(features), 256)
        ))


def FitBinaryTcn(
    training: TrainingSampleMatrix, parameters: TcnFittingParameters, threads: int, check: Callable[[], None],
) -> FittedBinaryTcn:
    import torch
    from torch.nn.functional import binary_cross_entropy_with_logits

    from trader.training.infra.tcn.network import ConfigureTcnRuntime, TcnNetwork

    if parameters.epochs != 10:
        raise ValueError("binary_probability_v5_requires_10_epochs")
    ConfigureTcnRuntime(training=True, compute_threads=threads)
    auxiliary_width = training.features.shape[1] - SEQUENCE_STEPS * len(SEQUENCE_CHANNELS)
    means, scales = FitTcnFeatureTransforms(training, auxiliary_width, parameters.batch_size, check)
    torch.manual_seed(parameters.seed)
    network = TcnNetwork(auxiliary_width, parameters.dropout, parameters.architecture)
    optimizer = torch.optim.AdamW(network.parameters(), lr=parameters.learning_rate, weight_decay=parameters.weight_decay)
    network.train()
    for _ in range(parameters.epochs):
        for start in range(0, len(training.labels), parameters.batch_size):
            check()
            end = start + parameters.batch_size
            inputs = torch.from_numpy((training.features[start:end] - means) / scales).to(network.compute_dtype)
            targets = torch.from_numpy(training.labels[start:end].copy()).to(network.compute_dtype)
            optimizer.zero_grad(set_to_none=True)
            loss = binary_cross_entropy_with_logits(network(inputs), targets)
            if not torch.isfinite(loss):
                raise ValueError("binary_probability_tcn_loss_nonfinite")
            loss.backward()
            optimizer.step()
    network.eval()
    return FittedBinaryTcn(network, means, scales)
