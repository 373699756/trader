"""Reusable deterministic paired moving-block statistics for preregistered families."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class PreregisteredBootstrapResult:
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
class PreregisteredHolmDecision:
    challenger_id: str
    p_value: float | None
    family_rank: int
    threshold: float
    rejected_null: bool


@dataclass(frozen=True)
class PreregisteredBootstrapPlan:
    identity: str
    master_seed: int
    challenger_id: str
    block_days: int
    repetitions: int

    def __post_init__(self) -> None:
        if not self.identity or not self.challenger_id:
            raise ValueError("paired bootstrap plan identity is required")
        if self.master_seed < 1 or self.block_days < 1 or self.repetitions < 1:
            raise ValueError("paired bootstrap plan parameters must be positive")

    @property
    def seed(self) -> int:
        return preregistered_seed(self.identity, self.master_seed, self.challenger_id, self.block_days)


def preregistered_seed(identity: str, master_seed: int, challenger_id: str, block_days: int) -> int:
    value = f"{identity}|{master_seed}|{challenger_id}|{block_days}"
    return int.from_bytes(hashlib.sha256(value.encode()).digest()[:8], "big", signed=False)


def paired_moving_block_statistics(
    values: tuple[float, ...],
    *,
    paired_metric: tuple[float, ...] | None = None,
    plan: PreregisteredBootstrapPlan,
) -> PreregisteredBootstrapResult:
    if any(not math.isfinite(value) for value in values):
        raise ValueError("paired bootstrap inputs must be finite")
    if paired_metric is not None and (
        len(paired_metric) != len(values) or any(not math.isfinite(value) for value in paired_metric)
    ):
        raise ValueError("paired bootstrap aligned metric is invalid")
    seed = plan.seed
    observed = math.fsum(values) / len(values) if values else None
    paired_observed = math.fsum(paired_metric) / len(paired_metric) if paired_metric else None
    if len(values) < plan.block_days:
        return PreregisteredBootstrapResult(
            plan.block_days,
            seed,
            plan.repetitions,
            len(values),
            observed,
            None,
            None,
            None,
            None,
            paired_observed,
            None,
            None,
            False,
            "sample_shorter_than_block",
        )
    if observed is None:
        raise AssertionError("validated bootstrap values cannot be empty")
    centered = tuple(value - observed for value in values)
    rng = random.Random(seed)
    sample_means: list[float] = []
    null_means: list[float] = []
    paired_means: list[float] = []
    block_starts = len(values) - plan.block_days + 1
    for _index in range(plan.repetitions):
        sampled: list[int] = []
        while len(sampled) < len(values):
            start = rng.randrange(block_starts)
            sampled.extend(range(start, start + plan.block_days))
        selected = sampled[: len(values)]
        sample_means.append(math.fsum(values[index] for index in selected) / len(values))
        null_means.append(math.fsum(centered[index] for index in selected) / len(values))
        if paired_metric is not None:
            paired_means.append(math.fsum(paired_metric[index] for index in selected) / len(values))
    extreme_count = sum(value >= observed for value in null_means)
    return PreregisteredBootstrapResult(
        plan.block_days,
        seed,
        plan.repetitions,
        len(values),
        observed,
        _nearest_rank(sample_means, 0.025),
        _nearest_rank(sample_means, 0.975),
        (extreme_count + 1) / (plan.repetitions + 1),
        extreme_count,
        paired_observed,
        _nearest_rank(paired_means, 0.025) if paired_means else None,
        _nearest_rank(paired_means, 0.975) if paired_means else None,
        True,
        None,
    )


def fixed_family_holm(
    p_values: Mapping[str, float | None],
    *,
    family: tuple[str, ...],
    alpha: float,
) -> tuple[PreregisteredHolmDecision, ...]:
    if len(family) != len(set(family)) or set(p_values) != set(family):
        raise ValueError("Holm input must contain the complete preregistered family")
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError("Holm alpha must be in (0, 1)")
    if any(value is not None and (not math.isfinite(value) or not 0.0 <= value <= 1.0) for value in p_values.values()):
        raise ValueError("Holm p-values must be finite and in [0, 1]")
    ordered = sorted(family, key=lambda challenger: (_p_order(p_values[challenger]), challenger))
    accepting = True
    decisions: list[PreregisteredHolmDecision] = []
    for family_rank, challenger_id in enumerate(ordered, start=1):
        threshold = alpha / (len(family) - family_rank + 1)
        p_value = p_values[challenger_id]
        rejected = accepting and p_value is not None and p_value <= threshold
        if not rejected:
            accepting = False
        decisions.append(PreregisteredHolmDecision(challenger_id, p_value, family_rank, threshold, rejected))
    return tuple(decisions)


def newey_west_long_run_std(values: tuple[float, ...], *, lag_days: int) -> float:
    """Return the Bartlett-kernel long-run standard deviation for historical rows."""

    if len(values) <= lag_days or lag_days < 1 or any(not math.isfinite(value) for value in values):
        raise ValueError("Newey-West inputs are invalid")
    mean = math.fsum(values) / len(values)
    centered = tuple(value - mean for value in values)
    long_run_variance = math.fsum(value * value for value in centered) / len(centered)
    for lag in range(1, lag_days + 1):
        covariance = math.fsum(centered[index] * centered[index - lag] for index in range(lag, len(centered)))
        covariance /= len(centered)
        long_run_variance += 2.0 * (1.0 - lag / (lag_days + 1.0)) * covariance
    if not math.isfinite(long_run_variance) or long_run_variance <= 0.0:
        raise ValueError("Newey-West long-run variance is not positive")
    return math.sqrt(long_run_variance)


def _p_order(value: float | None) -> float:
    return math.inf if value is None else value


def _nearest_rank(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    rank = max(1, math.ceil(probability * len(ordered)))
    return ordered[rank - 1]


__all__ = [
    "PreregisteredBootstrapResult",
    "PreregisteredBootstrapPlan",
    "PreregisteredHolmDecision",
    "fixed_family_holm",
    "newey_west_long_run_std",
    "paired_moving_block_statistics",
    "preregistered_seed",
]
