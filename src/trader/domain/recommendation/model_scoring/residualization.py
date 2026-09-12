"""Pure exposure residualization shared by production and research scoring."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
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
class ExposureContext:
    boards: tuple[str, ...]
    average_amounts: tuple[float, ...]
    industries: tuple[str, ...] | None
    contract: ExposureContract
    board_groups: tuple[tuple[int, ...], ...] = field(init=False)
    industry_groups: tuple[tuple[int, ...], ...] = field(init=False)
    amount_exposure: tuple[float, ...] = field(init=False)

    def __post_init__(self) -> None:
        size = len(self.boards)
        if not self.boards or size != len(self.average_amounts) or any(not board for board in self.boards):
            raise ValueError("scoring exposure vectors must have the same non-empty length")
        if any(not math.isfinite(amount) or amount <= 0.0 for amount in self.average_amounts):
            raise ValueError("scoring exposure values must be finite and amounts positive")
        if self.contract.requires_industry and (
            self.industries is None or len(self.industries) != size or any(not industry for industry in self.industries)
        ):
            raise ValueError("scoring exposure industries must have the same non-empty length")
        board_groups = _group_indices(self.boards)
        industry_groups = (
            _group_indices(_required_industries(self.industries)) if self.contract.requires_industry else ()
        )
        amount_exposure = _center_groups_by_indices(
            tuple(math.log(amount) for amount in self.average_amounts), board_groups
        )
        if self.contract.requires_industry:
            amount_exposure = _center_groups_by_indices(amount_exposure, industry_groups)
        object.__setattr__(self, "board_groups", board_groups)
        object.__setattr__(self, "industry_groups", industry_groups)
        object.__setattr__(self, "amount_exposure", amount_exposure)


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
    context = create_exposure_context(boards, average_amounts, industries=industries, contract=contract)
    return residualize_exposure_with_context(values, context)


def create_exposure_context(
    boards: Sequence[str],
    average_amounts: Sequence[float],
    *,
    industries: Sequence[str] | None = None,
    contract: ExposureContract = V1_V2_EXPOSURE_CONTRACT,
) -> ExposureContext:
    board_values = tuple(boards)
    amount_values = tuple(float(value) for value in average_amounts)
    industry_values = tuple(industries) if industries is not None else None
    return ExposureContext(board_values, amount_values, industry_values, contract)


def residualize_exposure_with_context(
    values: Sequence[float],
    context: ExposureContext,
) -> tuple[float, ...]:
    _validate_values(values, context)

    residuals = tuple(float(value) for value in values)
    for dimension in context.contract.order:
        residuals = _apply_dimension(residuals, dimension, context)
    return residuals


def _validate_values(
    values: Sequence[float],
    context: ExposureContext,
) -> None:
    if not values or len(values) != len(context.boards):
        raise ValueError("scoring exposure vectors must have the same non-empty length")
    if any(not math.isfinite(value) for value in values):
        raise ValueError("scoring exposure values must be finite and amounts positive")


def _apply_dimension(
    values: Sequence[float],
    dimension: ExposureDimension,
    context: ExposureContext,
) -> tuple[float, ...]:
    if dimension == "market":
        return _center_market(values)
    if dimension == "board":
        return _center_groups_by_indices(values, context.board_groups)
    if dimension == "industry":
        return _center_groups_by_indices(values, context.industry_groups)
    return _remove_linear_exposure(values, context.amount_exposure)


def _required_industries(industries: Sequence[str] | None) -> Sequence[str]:
    if industries is None:
        raise ValueError("scoring exposure industries are required")
    return industries


def _center_market(values: Sequence[float]) -> tuple[float, ...]:
    mean = math.fsum(values) / len(values)
    return tuple(value - mean for value in values)


def _group_indices(groups: Sequence[str]) -> tuple[tuple[int, ...], ...]:
    indices_by_group: dict[str, list[int]] = {}
    for index, group in enumerate(groups):
        indices_by_group.setdefault(group, []).append(index)
    return tuple(tuple(indices) for indices in indices_by_group.values())


def _center_groups_by_indices(
    values: Sequence[float],
    grouped_indices: Sequence[Sequence[int]],
) -> tuple[float, ...]:
    result = [0.0] * len(values)
    for indices in grouped_indices:
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
    "ExposureContext",
    "ExposureDimension",
    "V1_V2_EXPOSURE_CONTRACT",
    "V3_EXPOSURE_CONTRACT",
    "create_exposure_context",
    "residualize_exposure",
    "residualize_exposure_with_context",
]
