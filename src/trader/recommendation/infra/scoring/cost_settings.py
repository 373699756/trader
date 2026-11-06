"""Parse the online execution-cost policy at the recommendation boundary."""

from __future__ import annotations

from pathlib import Path

from trader.infra.settings.parser import Number, ReadJsonObject, RequireExactKeys
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy


def LoadExecutionCostPolicy(config_dir: Path) -> ExecutionCostPolicy:
    document = ReadJsonObject(config_dir / "cost.json")
    RequireExactKeys(
        document,
        {"online_round_trip_base_rate", "online_liquidity_rank_multiplier", "evaluation_round_trip_bps"},
        "cost",
    )
    return ExecutionCostPolicy(
        Number(document, "online_round_trip_base_rate", minimum=0.0),
        Number(document, "online_liquidity_rank_multiplier", minimum=0.0),
    )


__all__ = ["LoadExecutionCostPolicy"]
