"""Pure exposure residualization shared by production and research scoring."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

ExposureDimension = Literal["market", "board", "industry", "log_average_amount_20d"]


@dataclass(frozen=True)
class ExposureContract:
    """Ordered categorical and continuous exposures removed from an alpha."""

    order: tuple[ExposureDimension, ...]

    def __post_init__(self) -> None:
        allowed_orders = {
            ("market", "board", "log_average_amount_20d"),
            ("market", "board", "industry", "log_average_amount_20d"),
        }
        if self.order not in allowed_orders:
            raise ValueError("scoring exposure contract order is invalid")

    @property
    def requires_industry(self) -> bool:
        return "industry" in self.order


@dataclass(frozen=True)
class _ExposureContext:
    boards: Sequence[str]
    average_amounts: Sequence[float]
    industries: Sequence[str] | None
    contract: ExposureContract


V1_V2_EXPOSURE_CONTRACT = ExposureContract(("market", "board", "log_average_amount_20d"))
V3_EXPOSURE_CONTRACT = ExposureContract(("market", "board", "industry", "log_average_amount_20d"))


def residualize_exposure(
    values: Sequence[float],
    boards: Sequence[str],
    average_amounts: Sequence[float],
    *,
    industries: Sequence[str] | None = None,
    contract: ExposureContract = V1_V2_EXPOSURE_CONTRACT,
) -> tuple[float, ...]:
    size = len(values)
    context = _ExposureContext(boards, average_amounts, industries, contract)
    _validate_vectors(values, context, size)

    residuals = tuple(float(value) for value in values)
    for dimension in contract.order:
        residuals = _apply_dimension(residuals, dimension, context)
    return residuals


def _validate_vectors(
    values: Sequence[float],
    context: _ExposureContext,
    size: int,
) -> None:
    if not values or size != len(context.boards) or size != len(context.average_amounts):
        raise ValueError("scoring exposure vectors must have the same non-empty length")
    if any(not board for board in context.boards):
        raise ValueError("scoring exposure boards must be non-empty")
    if any(not math.isfinite(value) for value in values) or any(
        not math.isfinite(amount) or amount <= 0.0 for amount in context.average_amounts
    ):
        raise ValueError("scoring exposure values must be finite and amounts positive")
    if context.contract.requires_industry and (
        context.industries is None
        or len(context.industries) != size
        or any(not industry for industry in context.industries)
    ):
        raise ValueError("scoring exposure industries must have the same non-empty length")


def _apply_dimension(
    values: Sequence[float],
    dimension: ExposureDimension,
    context: _ExposureContext,
) -> tuple[float, ...]:
    if dimension == "market":
        return _center_market(values)
    if dimension == "board":
        return _center_groups(values, context.boards)
    if dimension == "industry":
        return _center_groups(values, _required_industries(context.industries))
    amount_exposure = _center_groups(tuple(math.log(amount) for amount in context.average_amounts), context.boards)
    if context.contract.requires_industry:
        amount_exposure = _center_groups(amount_exposure, _required_industries(context.industries))
    return _remove_linear_exposure(values, amount_exposure)


def _required_industries(industries: Sequence[str] | None) -> Sequence[str]:
    if industries is None:
        raise ValueError("scoring exposure industries are required")
    return industries


def _center_market(values: Sequence[float]) -> tuple[float, ...]:
    mean = math.fsum(values) / len(values)
    return tuple(value - mean for value in values)


def _center_groups(values: Sequence[float], groups: Sequence[str]) -> tuple[float, ...]:
    indices_by_group: dict[str, list[int]] = defaultdict(list)
    for index, group in enumerate(groups):
        indices_by_group[group].append(index)
    result = [0.0] * len(values)
    for indices in indices_by_group.values():
        mean = math.fsum(values[index] for index in indices) / len(indices)
        for index in indices:
            result[index] = values[index] - mean
    return tuple(result)


def _remove_linear_exposure(values: Sequence[float], exposure: Sequence[float]) -> tuple[float, ...]:
    denominator = math.fsum(value * value for value in exposure)
    slope = (
        math.fsum(value * amount for value, amount in zip(values, exposure, strict=True)) / denominator
        if denominator > 0.0
        else 0.0
    )
    return tuple(value - slope * amount for value, amount in zip(values, exposure, strict=True))


__all__ = [
    "ExposureContract",
    "ExposureDimension",
    "V1_V2_EXPOSURE_CONTRACT",
    "V3_EXPOSURE_CONTRACT",
    "residualize_exposure",
]
