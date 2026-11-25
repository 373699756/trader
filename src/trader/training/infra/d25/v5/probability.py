"""D25 V5 industry 10-epoch BCE TCN fit on the matured D25 hit event."""

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass

import numpy as np

from trader.recommendation.application.ports.loaded_profile import ModelInput
from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
from trader.training.infra.d25.artifact_values import D25ArtifactObject, D25ArtifactVector
from trader.training.infra.sample_index import TrainingSampleMatrix
from trader.training.infra.tcn.binary_fitting import FitBinaryTcn
from trader.training.infra.tcn.manifest import SEQUENCE_FEATURE_IDS
from trader.training.infra.tcn.settings import TCN_NETWORK_CONTRACT, TcnArchitecture, TcnFittingParameters
from trader.training.infra.tcn.tensors import DecodeTcnNetwork, TcnTensor

ALGORITHM = "tcn_bce"


@dataclass(frozen=True, slots=True)
class D25TcnModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    dropout: float
    tensors: tuple[TcnTensor, ...]
    architecture: TcnArchitecture


def FitD25Tcn(
    training: TrainingSampleMatrix, parameters: TcnFittingParameters, threads: int, check: Callable[[], None],
    *, report: Callable[[int, int, int], None] | None = None,
) -> tuple[D25TcnModel, Callable[[np.ndarray], np.ndarray]]:
    if parameters.epochs != 10:
        raise ValueError("d25_probability_v5_requires_10_epochs")
    fitted = FitBinaryTcn(training, parameters, threads, check, report=report)
    return (
        D25TcnModel(tuple(fitted.means), tuple(fitted.scales), parameters.dropout,
                    fitted.network.tensors(), parameters.architecture), fitted.logits,
    )


def EncodeD25Tcn(model: D25TcnModel) -> dict[str, object]:
    return {
        "means": model.means, "scales": model.scales,
        "network": {
            "contract": SEQUENCE_CONTRACT, "architecture": TCN_NETWORK_CONTRACT,
            "hidden_channels": model.architecture.hidden_channels, "compute_dtype": model.architecture.compute_dtype,
            "dropout": model.dropout, "weights": tuple(asdict(tensor) for tensor in model.tensors),
        },
    }


def DecodeD25Tcn(value: object, width: int) -> D25TcnModel:
    raw = D25ArtifactObject(value, {"means", "scales", "network"})
    means, scales = D25ArtifactVector(raw["means"], width), D25ArtifactVector(raw["scales"], width)
    if min(scales) <= 0:
        raise ValueError("d25_probability_transform_invalid")
    dropout, tensors, architecture = DecodeTcnNetwork(raw["network"], width - len(SEQUENCE_FEATURE_IDS))
    return D25TcnModel(means, scales, dropout, tensors, architecture)


def LoadD25TcnScorer(model: D25TcnModel, auxiliary_width: int) -> Callable[[Sequence[ModelInput]], np.ndarray]:
    from trader.training.infra.tcn.network import TcnNetwork

    network = TcnNetwork(auxiliary_width, model.dropout, model.architecture)
    network.load_tensors(model.tensors)
    network.eval()
    means, scales = np.asarray(model.means), np.asarray(model.scales)

    def scores(items: Sequence[ModelInput]) -> np.ndarray:
        if any(item.sequence is None for item in items):
            raise ValueError("d25_probability_sequence_missing")
        values = np.asarray([(*item.alpha_features, *item.sequence.values) for item in items if item.sequence is not None])
        return network.predict((values - means) / scales)

    return scores
