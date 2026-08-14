from __future__ import annotations

import hashlib

import pytest

from trader.domain.research.statistics import (
    BOOTSTRAP_REPETITIONS,
    bootstrap_seed,
    holm_step_down,
    paired_moving_block_bootstrap,
)


def test_r5_bootstrap_uses_preregistered_seed_and_is_deterministic() -> None:
    expected = int.from_bytes(
        hashlib.sha256(b"score_p0_v1|20260811|continuous_entry|5").digest()[:8],
        "big",
        signed=False,
    )
    values = tuple(0.001 * index for index in range(1, 41))
    drawdown = tuple(-0.01 if index % 2 else 0.0 for index in range(40))

    first = paired_moving_block_bootstrap(values, drawdown, "continuous_entry", 5)
    second = paired_moving_block_bootstrap(values, drawdown, "continuous_entry", 5)

    assert bootstrap_seed("continuous_entry", 5) == expected
    assert first == second
    assert first.seed == expected
    assert first.repetitions == BOOTSTRAP_REPETITIONS == 10_000
    assert first.valid is True
    assert first.confidence_lower <= first.observed_mean <= first.confidence_upper
    assert first.p_value is not None
    assert first.p_value == pytest.approx((first.extreme_count + 1) / 10_001)
    assert first.paired_metric_confidence_lower is not None


def test_r5_bootstrap_refuses_short_non_circular_blocks() -> None:
    result = paired_moving_block_bootstrap((0.1, 0.2), (0.0, 0.0), "combined_v1", 3)

    assert result.valid is False
    assert result.p_value is None
    assert result.confidence_lower is None
    assert result.invalid_reason == "sample_shorter_than_block"


def test_r5_holm_keeps_the_fixed_five_variant_family_after_first_failure() -> None:
    decisions = holm_step_down(
        {
            "continuous_entry": 0.009,
            "coverage_shrink": 0.020,
            "candidate_upper_bound": None,
            "heat_weak_structure": 0.001,
            "combined_v1": 0.021,
        }
    )
    by_id = {item.variant_id: item for item in decisions}

    assert tuple(item.family_rank for item in decisions) == (1, 2, 3, 4, 5)
    assert by_id["heat_weak_structure"].rejected_null is True
    assert by_id["continuous_entry"].rejected_null is True
    assert by_id["coverage_shrink"].rejected_null is False
    assert by_id["combined_v1"].rejected_null is False
    assert by_id["candidate_upper_bound"].rejected_null is False
    assert by_id["candidate_upper_bound"].p_value is None
