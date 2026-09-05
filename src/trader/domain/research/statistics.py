"""Pure preregistered Score-R5 paired statistics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import cast

from trader.domain.research.challengers import ChallengerVariantId
from trader.domain.research.paired_statistics import (
    PreregisteredBootstrapPlan,
    fixed_family_holm,
    paired_moving_block_statistics,
    preregistered_seed,
)
from trader.domain.research.specification import SCORE_P0_V1_SPEC, ScoreResearchSpec

BOOTSTRAP_MASTER_SEED = 20260811
BOOTSTRAP_REPETITIONS = 10_000
BOOTSTRAP_BLOCK_DAYS = (3, 5, 10)
PRIMARY_BLOCK_DAYS = 5
HOLM_ALPHA = 0.05
VARIANT_FAMILY: tuple[ChallengerVariantId, ...] = (
    "continuous_entry",
    "coverage_shrink",
    "candidate_upper_bound",
    "heat_weak_structure",
    "combined",
)


@dataclass(frozen=True)
class PairedBootstrapResult:
    block_days: int
    seed: int
    repetitions: int
    sample_count: int
    observed_mean: float | None
    confidence_lower: float | None
    confidence_upper: float | None
    p_value: float | None
    extreme_count: int | None
    paired_metric_observed_mean: float | None
    paired_metric_confidence_lower: float | None
    paired_metric_confidence_upper: float | None
    valid: bool
    invalid_reason: str | None


@dataclass(frozen=True)
class HolmDecision:
    variant_id: ChallengerVariantId
    p_value: float | None
    family_rank: int
    threshold: float
    rejected_null: bool


def bootstrap_seed(
    variant_id: ChallengerVariantId,
    block_days: int,
    *,
    spec: ScoreResearchSpec = SCORE_P0_V1_SPEC,
) -> int:
    if variant_id not in VARIANT_FAMILY:
        raise ValueError("Score-R5 bootstrap requires a preregistered variant")
    if block_days not in BOOTSTRAP_BLOCK_DAYS:
        raise ValueError("Score-R5 bootstrap requires a preregistered block length")
    return preregistered_seed(spec.research_identity, spec.bootstrap_master_seed, variant_id, block_days)


def paired_moving_block_bootstrap(
    values: tuple[float, ...],
    paired_metric: tuple[float, ...],
    variant_id: ChallengerVariantId,
    block_days: int,
    *,
    spec: ScoreResearchSpec = SCORE_P0_V1_SPEC,
) -> PairedBootstrapResult:
    """Bootstrap two aligned daily metrics with identical non-circular block indices."""

    _validate_series(values, paired_metric)
    result = paired_moving_block_statistics(
        values,
        paired_metric=paired_metric,
        plan=PreregisteredBootstrapPlan(
            identity=spec.research_identity,
            master_seed=spec.bootstrap_master_seed,
            challenger_id=variant_id,
            block_days=block_days,
            repetitions=BOOTSTRAP_REPETITIONS,
        ),
    )
    return PairedBootstrapResult(
        result.block_days,
        result.seed,
        result.repetitions,
        result.sample_count,
        result.observed_mean,
        result.confidence_lower,
        result.confidence_upper,
        result.p_value,
        result.extreme_count,
        result.paired_metric_observed_mean,
        result.paired_metric_confidence_lower,
        result.paired_metric_confidence_upper,
        result.valid,
        result.invalid_reason,
    )


def holm_step_down(
    p_values: dict[ChallengerVariantId, float | None],
) -> tuple[HolmDecision, ...]:
    """Apply the fixed five-member Holm family without dropping invalid variants."""

    if set(p_values) != set(VARIANT_FAMILY):
        raise ValueError("Score-R5 Holm input must contain the fixed five-variant family")
    for value in p_values.values():
        if value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0):
            raise ValueError("Score-R5 Holm p-values must be finite and in [0, 1]")
    generic_p_values: dict[str, float | None] = {str(key): value for key, value in p_values.items()}
    decisions = fixed_family_holm(generic_p_values, family=VARIANT_FAMILY, alpha=HOLM_ALPHA)
    return tuple(
        HolmDecision(
            variant_id=cast(ChallengerVariantId, decision.challenger_id),
            p_value=decision.p_value,
            family_rank=decision.family_rank,
            threshold=decision.threshold,
            rejected_null=decision.rejected_null,
        )
        for decision in decisions
    )


def _validate_series(values: tuple[float, ...], paired_metric: tuple[float, ...]) -> None:
    if len(values) != len(paired_metric):
        raise ValueError("Score-R5 paired bootstrap series must have identical lengths")
    if any(not math.isfinite(value) for value in (*values, *paired_metric)):
        raise ValueError("Score-R5 paired bootstrap inputs must be finite")


__all__ = [
    "BOOTSTRAP_BLOCK_DAYS",
    "BOOTSTRAP_MASTER_SEED",
    "BOOTSTRAP_REPETITIONS",
    "HOLM_ALPHA",
    "PRIMARY_BLOCK_DAYS",
    "VARIANT_FAMILY",
    "HolmDecision",
    "PairedBootstrapResult",
    "bootstrap_seed",
    "holm_step_down",
    "paired_moving_block_bootstrap",
]
