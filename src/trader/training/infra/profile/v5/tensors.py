"""Strict portable tensor codec; parsing never imports or executes PyTorch."""

import math
from dataclasses import dataclass

from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT


@dataclass(frozen=True)
class TcnTensor:
    name: str
    shape: tuple[int, ...]
    values: tuple[float, ...]


def TensorShapes(auxiliary_width: int) -> tuple[tuple[str, tuple[int, ...]], ...]:
    if auxiliary_width not in (5, 6):
        raise ValueError("V5 auxiliary width must be five or six")
    shapes: list[tuple[str, tuple[int, ...]]] = []
    for block in range(4):
        for layer, channels, kernel in (("first", 6 if block == 0 else 16, 3), ("second", 16, 3)):
            shapes.extend(
                ((f"blocks.{block}.{layer}.weight", (16, channels, kernel)), (f"blocks.{block}.{layer}.bias", (16,)))
            )
        if block == 0:
            shapes.extend((("blocks.0.shortcut.weight", (16, 6, 1)), ("blocks.0.shortcut.bias", (16,))))
    return (*shapes, ("output.weight", (1, 16 + auxiliary_width)), ("output.bias", (1,)))


def DecodeTcnTensors(value: object, auxiliary_width: int) -> tuple[TcnTensor, ...]:
    shapes = dict(TensorShapes(auxiliary_width))
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
            or any(type(v) not in (float, int) or not math.isfinite(v) for v in values)
        ):
            raise ValueError("V5 tensor identity, dimensions or values are invalid")
        tensors.append(TcnTensor(name, shapes.pop(name), tuple(float(v) for v in values)))
    if shapes:
        raise ValueError("V5 tensors are incomplete")
    return tuple(sorted(tensors, key=lambda t: t.name))


def DecodeTcnNetwork(value: object, auxiliary_width: int) -> tuple[float, tuple[TcnTensor, ...]]:
    if not isinstance(value, dict) or set(value) != {"contract", "dropout", "weights"}:
        raise ValueError("V5 network fields are invalid")
    dropout = value["dropout"]
    if (
        value["contract"] != SEQUENCE_CONTRACT
        or type(dropout) not in (int, float)
        or not math.isfinite(dropout)
        or not 0 <= dropout < 1
    ):
        raise ValueError("V5 network contract is invalid")
    return float(dropout), DecodeTcnTensors(value["weights"], auxiliary_width)


__all__ = ["TcnTensor", "TensorShapes", "DecodeTcnTensors", "DecodeTcnNetwork"]
