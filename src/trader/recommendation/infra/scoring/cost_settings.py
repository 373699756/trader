"""Parse the online execution-cost policy at the recommendation boundary."""

from __future__ import annotations

from pathlib import Path

from trader.infra.settings.parser import number, read_json_object, require_exact_keys
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy, ExecutionCostScenario


def load_execution_cost_policy(config_dir: Path) -> ExecutionCostPolicy:
    document = read_json_object(config_dir / "cost.json")
    require_exact_keys(
        document,
        {"online_round_trip_base_rate", "online_liquidity_rank_multiplier", "evaluation_round_trip_bps"},
        "cost",
    )
    return ExecutionCostPolicy(
        number(document, "online_round_trip_base_rate", minimum=0.0),
        number(document, "online_liquidity_rank_multiplier", minimum=0.0),
    )


def load_evaluation_cost_scenarios(config_dir: Path) -> tuple[ExecutionCostScenario, ...]:
    document = read_json_object(config_dir / "cost.json")
    require_exact_keys(
        document,
        {"online_round_trip_base_rate", "online_liquidity_rank_multiplier", "evaluation_round_trip_bps"},
        "cost",
    )
    raw = document.get("evaluation_round_trip_bps")
    bps_values = (20, 50, 100)
    if raw != list(bps_values):
        raise ValueError("cost.evaluation_round_trip_bps must preserve the pre-registered 20/50/100bp scenarios")
    return tuple(ExecutionCostScenario(f"cost_{bps}bp", bps / 10_000) for bps in bps_values)


__all__ = ["load_evaluation_cost_scenarios", "load_execution_cost_policy"]
