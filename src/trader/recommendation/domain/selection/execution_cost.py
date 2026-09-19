"""Typed execution-cost and capacity estimates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

_SCENARIO_ID = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True, order=True)
class ExecutionCostScenario:
    scenario_id: str
    round_trip_return: float

    def __post_init__(self) -> None:
        if _SCENARIO_ID.fullmatch(self.scenario_id) is None:
            raise ValueError("execution-cost scenario identity is invalid")
        if not math.isfinite(self.round_trip_return) or self.round_trip_return < 0.0:
            raise ValueError("execution-cost scenario value is invalid")


@dataclass(frozen=True)
class ExecutionCost:
    code: str
    estimated_round_trip_return: float
    capacity_amount: float | None
    participation_rate: float | None
    scenarios: tuple[ExecutionCostScenario, ...]

    def __post_init__(self) -> None:
        if len(self.code) != 6 or not self.code.isdigit():
            raise ValueError("execution-cost code must contain exactly six digits")
        if not math.isfinite(self.estimated_round_trip_return) or self.estimated_round_trip_return < 0.0:
            raise ValueError("execution-cost estimate is invalid")
        if self.capacity_amount is not None and (
            not math.isfinite(self.capacity_amount) or self.capacity_amount <= 0.0
        ):
            raise ValueError("execution capacity must be positive when present")
        if self.participation_rate is not None and (
            not math.isfinite(self.participation_rate) or not 0.0 <= self.participation_rate <= 1.0
        ):
            raise ValueError("execution participation rate must be in [0, 1]")
        scenarios = tuple(sorted(self.scenarios))
        if not scenarios or len({item.scenario_id for item in scenarios}) != len(scenarios):
            raise ValueError("execution-cost scenarios must be non-empty and unique")
        object.__setattr__(self, "scenarios", scenarios)


__all__ = ["ExecutionCost", "ExecutionCostScenario"]
