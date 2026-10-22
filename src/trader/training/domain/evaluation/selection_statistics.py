"""Preregistered date-level inference for the full-population selection study."""

from __future__ import annotations

from dataclasses import dataclass

from trader.training.domain.evaluation.paired_statistics import (
    FixedFamilyHolm,
    PairedMovingBlockStatistics,
    PreregisteredBootstrapPlan,
    PreregisteredBootstrapResult,
    PreregisteredHolmDecision,
)
from trader.training.domain.evaluation.selection_experiment import (
    PairedSelectionExperimentReport,
    PairedSelectionSpecification,
    SelectionExperimentResult,
)


@dataclass(frozen=True)
class SelectionInference:
    variant_id: str
    cost_bps: int
    statistics: PreregisteredBootstrapResult
    holm: PreregisteredHolmDecision


def EvaluateSelectionStatistics(
    specification: PairedSelectionSpecification, report: PairedSelectionExperimentReport
) -> tuple[SelectionInference, ...]:
    """Use paired parent dates, including cash days; 100bp remains stress only."""
    if report.specification_hash != specification.content_hash:
        raise ValueError("selection statistics require the exact preregistered specification")
    if report.status != "historical_experiment_completed":
        return ()
    results: list[tuple[str, int, PreregisteredBootstrapResult]] = []
    for result in report.results:
        variant = result.variant
        if variant.arm == "rule_only":
            continue
        control = next(
            item
            for item in report.results
            if (
                item.variant.arm == "rule_only"
                and item.variant.target == variant.target
                and item.variant.selection_mode == variant.selection_mode
            )
        )
        for index, cost_bps in enumerate((20, 50)):
            identity = _variant_id(result, cost_bps)
            values = tuple(day.six_slot_net_excess_returns[index] for day in result.days)
            increments = tuple(
                day.six_slot_net_excess_returns[index] - baseline.six_slot_net_excess_returns[index]
                for day, baseline in zip(result.days, control.days, strict=True)
            )
            statistics = PairedMovingBlockStatistics(
                values,
                paired_metric=increments,
                plan=PreregisteredBootstrapPlan(
                    specification.content_hash,
                    specification.bootstrap_master_seed,
                    identity,
                    specification.bootstrap_block_days,
                    specification.bootstrap_repetitions,
                ),
            )
            results.append((identity, cost_bps, statistics))
    decisions = FixedFamilyHolm(
        {identity: statistics.p_value for identity, _cost, statistics in results},
        family=tuple(identity for identity, _cost, _statistics in results),
        alpha=0.05,
    )
    by_identity = {item.challenger_id: item for item in decisions}
    return tuple(SelectionInference(identity, cost, stats, by_identity[identity]) for identity, cost, stats in results)


def _variant_id(result: SelectionExperimentResult, cost_bps: int) -> str:
    variant = result.variant
    return "_".join(
        (
            variant.arm,
            variant.pooled_model_family or "industry",
            variant.selection_mode,
            variant.target,
            f"{cost_bps}bp",
        )
    )


__all__ = ["EvaluateSelectionStatistics", "SelectionInference"]
