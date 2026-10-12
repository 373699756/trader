"""Strict pre-registered research settings kept outside production scoring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from trader.infra.settings.parser import integer, mapping, profile_mapping, read_json_object, require_exact_keys

_ARMS = (
    "current_chain",
    "without_input_completeness_bonus",
    "without_d25_execution_bonus",
    "without_tomorrow_flow_bonus",
    "without_preselection_stability_bonus",
    "without_d25_quality_valuation_bonus",
    "equal_weight_local_components",
    "fixed_20bp_round_trip_cost",
)


@dataclass(frozen=True)
class ResearchSettings:
    terminal_holdout_sessions: int
    tomorrow_embargo_sessions: int
    d25_embargo_sessions: int
    v2_fit_date_candidates: tuple[int, ...]
    v3_fit_date_candidates: tuple[int, ...]
    ablation_arms: tuple[str, ...]
    deepseek_comparison_arms: tuple[str, str, str]


def load_research_settings(config_dir: Path) -> ResearchSettings:
    document = read_json_object(config_dir / "research.json")
    require_exact_keys(
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
    embargo = mapping(document, "embargo_sessions")
    require_exact_keys(embargo, {"tomorrow", "d25"}, "research.embargo_sessions")
    candidates = document.get("fit_date_candidates")
    v2_profile = profile_mapping(candidates, "v2", "research.fit_date_candidates")
    v3_profile = profile_mapping(candidates, "v3", "research.fit_date_candidates")
    require_exact_keys(v2_profile, {"dates"}, "research.fit_date_candidates.v2")
    require_exact_keys(v3_profile, {"dates"}, "research.fit_date_candidates.v3")
    v2 = _positive_integers(v2_profile.get("dates"), "research.fit_date_candidates.v2")
    v3 = _positive_integers(v3_profile.get("dates"), "research.fit_date_candidates.v3")
    arms_raw = document.get("ablation_arms")
    if not isinstance(arms_raw, list) or tuple(arms_raw) != _ARMS:
        raise ValueError("research.ablation_arms must match the pre-registered eight-arm contract")
    deepseek_raw = document.get("deepseek_comparison_arms")
    deepseek_arms = ("local_only", "structured_facts_veto", "fixed_68_32")
    if not isinstance(deepseek_raw, list) or tuple(deepseek_raw) != deepseek_arms:
        raise ValueError("research.deepseek_comparison_arms must match the three-arm contract")
    return ResearchSettings(
        terminal_holdout_sessions=integer(document, "terminal_holdout_sessions", minimum=200),
        tomorrow_embargo_sessions=integer(embargo, "tomorrow", minimum=1),
        d25_embargo_sessions=integer(embargo, "d25", minimum=5),
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


__all__ = ["ResearchSettings", "load_research_settings"]
