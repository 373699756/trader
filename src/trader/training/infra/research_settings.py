"""Strict pre-registered research settings kept outside production scoring."""

from __future__ import annotations

from pathlib import Path

from trader.infra.settings.parser import Integer, ProfileMapping, ReadJsonObject, RequireExactKeys, RequireMapping
from trader.training.domain.evaluation.selection_experiment import SELECTION_ARMS
from trader.training.domain.evaluation.research_plan import ResearchExecutionPlan, ResearchSettings
from trader.recommendation.domain.selection.execution_cost import ExecutionCostScenario

_ARMS = SELECTION_ARMS


def LoadResearchSettings(config_dir: Path) -> ResearchSettings:
    document = ReadJsonObject(config_dir / "research.json")
    RequireExactKeys(
        document,
        {
            "terminal_holdout_sessions",
            "embargo_sessions",
            "fit_date_candidates",
            "ablation_arms",
            "deepseek_comparison_arms",
        },
        "research",
    )
    embargo = RequireMapping(document, "embargo_sessions")
    RequireExactKeys(embargo, {"tomorrow", "d25"}, "research.embargo_sessions")
    candidates = document.get("fit_date_candidates")
    v2_profile = ProfileMapping(candidates, "v2", "research.fit_date_candidates")
    v3_profile = ProfileMapping(candidates, "v3", "research.fit_date_candidates")
    RequireExactKeys(v2_profile, {"dates"}, "research.fit_date_candidates.v2")
    RequireExactKeys(v3_profile, {"dates"}, "research.fit_date_candidates.v3")
    v2 = _positive_integers(v2_profile.get("dates"), "research.fit_date_candidates.v2")
    v3 = _positive_integers(v3_profile.get("dates"), "research.fit_date_candidates.v3")
    arms_raw = document.get("ablation_arms")
    if not isinstance(arms_raw, list) or tuple(arms_raw) != _ARMS:
        raise ValueError("research.ablation_arms must match the full-population four-arm contract")
    deepseek_raw = document.get("deepseek_comparison_arms")
    deepseek_arms = ("local_only", "structured_facts_veto")
    if not isinstance(deepseek_raw, list) or tuple(deepseek_raw) != deepseek_arms:
        raise ValueError("research.deepseek_comparison_arms must match the structured-veto comparison")
    return ResearchSettings(
        terminal_holdout_sessions=Integer(document, "terminal_holdout_sessions", minimum=200),
        tomorrow_embargo_sessions=Integer(embargo, "tomorrow", minimum=5),
        d25_embargo_sessions=Integer(embargo, "d25", minimum=5),
        v2_fit_date_candidates=v2,
        v3_fit_date_candidates=v3,
        ablation_arms=_ARMS,
        deepseek_comparison_arms=deepseek_arms,
    )


def _positive_integers(raw: object, label: str) -> tuple[int, ...]:
    if (
        not isinstance(raw, list)
        or not raw
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in raw)
        or raw != sorted(set(raw))
    ):
        raise ValueError(f"{label} must contain sorted unique positive integers")
    return tuple(raw)


def LoadEvaluationCostScenarios(config_dir: Path) -> tuple[ExecutionCostScenario, ...]:
    document = ReadJsonObject(config_dir / "cost.json")
    RequireExactKeys(
        document,
        {"online_round_trip_base_rate", "online_liquidity_rank_multiplier", "evaluation_round_trip_bps"},
        "cost",
    )
    raw = document.get("evaluation_round_trip_bps")
    bps_values = (20, 50, 100)
    if raw != list(bps_values):
        raise ValueError("cost.evaluation_round_trip_bps must preserve the pre-registered 20/50/100bp scenarios")
    return tuple(ExecutionCostScenario(f"cost_{bps}bp", bps / 10_000) for bps in bps_values)


def LoadResearchPlan(config_dir: Path) -> ResearchExecutionPlan:
    return ResearchExecutionPlan(LoadResearchSettings(config_dir), LoadEvaluationCostScenarios(config_dir))


__all__ = [
    "LoadResearchSettings",
    "LoadEvaluationCostScenarios",
    "LoadResearchPlan",
]
