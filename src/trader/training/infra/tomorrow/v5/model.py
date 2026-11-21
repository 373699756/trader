"""Tomorrow V5 ten-epoch BCE classifier and native runtime adapter."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from importlib.util import find_spec
from typing import TYPE_CHECKING

import numpy as np

from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES, TOMORROW_SEQUENCE_FEATURE_NAMES
from trader.training.infra.sample_index import TrainingSampleMatrix
from trader.training.infra.tcn.binary_fitting import FittedBinaryTcn, FitBinaryTcn
from trader.training.infra.tcn.settings import TcnArchitecture, TcnFittingParameters
from trader.training.infra.tcn.tensors import TcnTensor

if TYPE_CHECKING:
    from trader.training.infra.tcn.network import TcnNetwork

ALGORITHM = "tcn_bce"


@dataclass(frozen=True, slots=True)
class TomorrowTcnModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    dropout: float
    tensors: tuple[TcnTensor, ...]
    architecture: TcnArchitecture

    def __post_init__(self) -> None:
        width = len(TOMORROW_SEQUENCE_FEATURE_NAMES)
        if (
            len(self.means) != width or len(self.scales) != width
            or not all(math.isfinite(value) for value in (*self.means, *self.scales))
            or any(value <= 0 for value in self.scales) or not 0 <= self.dropout < 1
        ):
            raise ValueError("tomorrow_tcn_transforms_invalid")


def FitTomorrowTcn(
    features: np.ndarray, labels: np.ndarray, parameters: TcnFittingParameters,
    check: Callable[[], None], threads: int,
) -> tuple[TomorrowTcnModel, FittedBinaryTcn]:
    if parameters.epochs != 10:
        raise ValueError("tomorrow_probability_v5_requires_10_epochs")
    fitted = FitBinaryTcn(TrainingSampleMatrix(features, labels), parameters, min(threads, parameters.compute_threads), check)
    return TomorrowTcnModel(
        tuple(fitted.means), tuple(fitted.scales), parameters.dropout,
        fitted.network.tensors(), parameters.architecture,
    ), fitted


def LoadTomorrowNetwork(model: TomorrowTcnModel) -> TcnNetwork:
    if find_spec("torch") is None:
        raise ValueError("tomorrow_v5_tensor_dependency_unavailable")
    try:
        from trader.training.infra.tcn.network import ConfigureTcnRuntime, TcnNetwork
    except ImportError as exc:
        raise ValueError("tomorrow_v5_tensor_dependency_unavailable") from exc

    ConfigureTcnRuntime(training=False)
    network = TcnNetwork(len(TOMORROW_FEATURE_NAMES), model.dropout, model.architecture)
    network.load_tensors(model.tensors)
    network.eval()
    return network
