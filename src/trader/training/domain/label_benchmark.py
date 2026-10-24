"""Engineering training benchmarks from observed returns at each fixed horizon."""

from __future__ import annotations

import math
from collections.abc import Iterable

TRAINING_LABEL_BENCHMARK_CONTRACT = "daily_close_observed_returns_equal_weight_per_horizon"


def ObservedTrainingBenchmark(returns: Iterable[float | None]) -> float | None:
    """Missing outcomes remove only that stock/horizon; never impute a future price."""
    observed = tuple(value for value in returns if value is not None)
    if any(not math.isfinite(value) for value in observed):
        raise ValueError("training_benchmark_return_non_finite")
    return math.fsum(observed) / len(observed) if observed else None


__all__ = ["ObservedTrainingBenchmark", "TRAINING_LABEL_BENCHMARK_CONTRACT"]
