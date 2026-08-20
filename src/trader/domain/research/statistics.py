"""Pure preregistered Score-R5 paired statistics."""

from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass

from trader.domain.research.challengers import ChallengerVariantId
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
    "combined_v1",
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
    identity = f"{spec.research_identity}|{spec.bootstrap_master_seed}|{variant_id}|{block_days}"
    return int.from_bytes(hashlib.sha256(identity.encode("utf-8")).digest()[:8], "big", signed=False)


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
    seed = bootstrap_seed(variant_id, block_days, spec=spec)
    if len(values) < block_days:
        return PairedBootstrapResult(
            block_days,
            seed,
            BOOTSTRAP_REPETITIONS,
            len(values),
            _mean(values),
            None,
            None,
            None,
            None,
            _mean(paired_metric),
            None,
            None,
            False,
            "sample_shorter_than_block",
        )
    observed = _mean(values)
    paired_observed = _mean(paired_metric)
    if observed is None or paired_observed is None:
        raise AssertionError("validated Score-R5 series cannot be empty")
    centered = tuple(value - observed for value in values)
    rng = random.Random(seed)
    sample_means: list[float] = []
    null_means: list[float] = []
    paired_means: list[float] = []
    block_starts = len(values) - block_days + 1
    for _index in range(BOOTSTRAP_REPETITIONS):
        sampled_indices: list[int] = []
        while len(sampled_indices) < len(values):
            start = rng.randrange(block_starts)
            sampled_indices.extend(range(start, start + block_days))
        sampled_indices = sampled_indices[: len(values)]
        sample_means.append(math.fsum(values[index] for index in sampled_indices) / len(values))
        null_means.append(math.fsum(centered[index] for index in sampled_indices) / len(values))
        paired_means.append(math.fsum(paired_metric[index] for index in sampled_indices) / len(values))
    extreme_count = sum(value >= observed for value in null_means)
    return PairedBootstrapResult(
        block_days,
        seed,
        BOOTSTRAP_REPETITIONS,
        len(values),
        observed,
        _nearest_rank(sample_means, 0.025),
        _nearest_rank(sample_means, 0.975),
        (extreme_count + 1) / (BOOTSTRAP_REPETITIONS + 1),
        extreme_count,
        paired_observed,
        _nearest_rank(paired_means, 0.025),
        _nearest_rank(paired_means, 0.975),
        True,
        None,
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
    ordered = sorted(VARIANT_FAMILY, key=lambda variant: (_p_order(p_values[variant]), variant))
    accepting = True
    decisions: list[HolmDecision] = []
    family_size = len(VARIANT_FAMILY)
    for family_rank, variant_id in enumerate(ordered, start=1):
        threshold = HOLM_ALPHA / (family_size - family_rank + 1)
        p_value = p_values[variant_id]
        rejected = accepting and p_value is not None and p_value <= threshold
        if not rejected:
            accepting = False
        decisions.append(HolmDecision(variant_id, p_value, family_rank, threshold, rejected))
    return tuple(decisions)


def _p_order(value: float | None) -> float:
    return math.inf if value is None else value


def _validate_series(values: tuple[float, ...], paired_metric: tuple[float, ...]) -> None:
    if len(values) != len(paired_metric):
        raise ValueError("Score-R5 paired bootstrap series must have identical lengths")
    if any(not math.isfinite(value) for value in (*values, *paired_metric)):
        raise ValueError("Score-R5 paired bootstrap inputs must be finite")


def _mean(values: tuple[float, ...]) -> float | None:
    return math.fsum(values) / len(values) if values else None


def _nearest_rank(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(probability * len(ordered)))
    return ordered[rank - 1]


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
