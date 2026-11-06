"""Strict portable tensor codec; parsing never imports or executes PyTorch."""

import math
from dataclasses import dataclass

from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
from trader.training.infra.profile.v5.settings import TCN_NETWORK_CONTRACT, TcnArchitecture


@dataclass(frozen=True)
class TcnTensor:
    name: str
    shape: tuple[int, ...]
    values: tuple[float, ...]


def TensorShapes(auxiliary_width: int, architecture: TcnArchitecture) -> tuple[tuple[str, tuple[int, ...]], ...]:
    if auxiliary_width not in (5, 6):
        raise ValueError("V5 auxiliary width must be five or six")
    shapes: list[tuple[str, tuple[int, ...]]] = []
    width = architecture.hidden_channels
    for block in range(4):
        for layer, channels, kernel in (("first", 6 if block == 0 else width, 3), ("second", width, 3)):
            shapes.extend(
                (
                    (f"blocks.{block}.{layer}.weight", (width, channels, kernel)),
                    (f"blocks.{block}.{layer}.bias", (width,)),
                )
            )
        if block == 0:
            shapes.extend((("blocks.0.shortcut.weight", (width, 6, 1)), ("blocks.0.shortcut.bias", (width,))))
    return (*shapes, ("output.weight", (1, width + auxiliary_width)), ("output.bias", (1,)))


def DecodeTcnTensors(value: object, auxiliary_width: int, architecture: TcnArchitecture) -> tuple[TcnTensor, ...]:
    shapes = dict(TensorShapes(auxiliary_width, architecture))
    if not isinstance(value, list) or len(value) != len(shapes):
        raise ValueError("V5 tensor count is invalid")
    tensors: list[TcnTensor] = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != {"name", "shape", "values"}:
            raise ValueError("V5 tensor fields are invalid")
        name, shape, values = raw["name"], raw["shape"], raw["values"]
        if (
            not isinstance(name, str)
            or name not in shapes
            or not isinstance(shape, list)
            or any(type(v) is not int for v in shape)
            or tuple(shape) != shapes[name]
            or not isinstance(values, list)
            or len(values) != math.prod(shapes[name])
            or any(
                type(v) not in (float, int)
                or not math.isfinite(v)
                or (architecture.compute_dtype == "float32" and abs(v) > 3.4028234663852886e38)
                for v in values
            )
        ):
            raise ValueError("V5 tensor identity, dimensions or values are invalid")
        tensors.append(TcnTensor(name, shapes.pop(name), tuple(float(v) for v in values)))
    if shapes:
        raise ValueError("V5 tensors are incomplete")
    return tuple(sorted(tensors, key=lambda t: t.name))


def DecodeTcnNetwork(value: object, auxiliary_width: int) -> tuple[float, tuple[TcnTensor, ...], TcnArchitecture]:
    if not isinstance(value, dict) or set(value) != {
        "contract",
        "architecture",
        "hidden_channels",
        "compute_dtype",
        "dropout",
        "weights",
    }:
        raise ValueError("V5 network structure identity missing or invalid; retraining required")
    channels, dtype = value["hidden_channels"], value["compute_dtype"]
    if (
        type(channels) is not int
        or dtype not in ("float32", "float64")
        or value["architecture"] != TCN_NETWORK_CONTRACT
    ):
        raise ValueError("V5 network architecture is invalid")
    architecture = TcnArchitecture(channels, "float32" if dtype == "float32" else "float64")
    dropout = value["dropout"]
    if (
        value["contract"] != SEQUENCE_CONTRACT
        or type(dropout) not in (int, float)
        or not math.isfinite(dropout)
        or not 0 <= dropout < 1
    ):
        raise ValueError("V5 network contract is invalid")
    return float(dropout), DecodeTcnTensors(value["weights"], auxiliary_width, architecture), architecture


__all__ = ["TcnTensor", "TensorShapes", "DecodeTcnTensors", "DecodeTcnNetwork"]
