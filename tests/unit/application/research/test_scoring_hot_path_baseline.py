from __future__ import annotations

import pytest

from trader.application.research.scoring_hot_path_baseline import (
    ScoringHotPathEquivalence,
    ScoringHotPathLatency,
    ScoringInputEpoch,
    build_scoring_hot_path_baseline,
)
from trader.domain.recommendation.models import Strategy


def _epoch(strategy: Strategy, *, candidates: int = 0, completed: bool = True) -> ScoringInputEpoch:
    return ScoringInputEpoch(
        strategy,
        "afternoon",
        f"test:{strategy.value}:epoch",
        (),
        candidates,
        candidates,
        candidates * 2,
        external_request_count=2,
        cache_hit_count=1,
        cache_miss_count=1,
        completed_before_freeze=completed,
    )


def test_empty_recommendation_keeps_candidate_and_epoch_denominators() -> None:
    report = build_scoring_hot_path_baseline(
        (_epoch(Strategy.TOMORROW),),
        equivalence=(
            ScoringHotPathEquivalence(
                "same_input",
                "passed",
                "0" * 64,
            ),
        ),
    )

    cost = report.slices[0].cost
    assert report.status == "passed"
    assert cost.completed_epoch_count == 1
    assert cost.evaluated_candidate_count == 0
    assert cost.cost_per_epoch == 2.0
    assert cost.cost_per_candidate == 0.0
    assert cost.cost_per_formal_decision == 0.0
    assert cost.cost_per_deepseek_candidate == 0.0


def test_baseline_groups_strategy_phase_and_records_freeze_completion_rate() -> None:
    report = build_scoring_hot_path_baseline(
        (_epoch(Strategy.TODAY, candidates=2), _epoch(Strategy.TODAY, candidates=1, completed=False)),
        latencies=(ScoringHotPathLatency("local_scoring", 1.0, 2.0, 3.0, 2),),
        formal_current_decision_count=1,
        formal_frozen_decision_count=1,
        deepseek_candidate_count=2,
        equivalence=(ScoringHotPathEquivalence("same_input", "passed", "0" * 64),),
    )

    item = report.slices[0]
    assert item.freeze_before_completion_rate == 0.5
    assert item.cost.evaluated_candidate_count == 3
    assert item.cost.formal_current_decision_count == 1
    assert item.latencies[0].p95_ms == 2.0
    assert item.recompute_shrink_ratio == 0.0


def test_epoch_rejects_invalid_changed_code_and_over_recomputation() -> None:
    with pytest.raises(ValueError, match="six digits"):
        _epoch(Strategy.TODAY).__class__(
            Strategy.TODAY,
            "afternoon",
            "test:today:epoch",
            ("bad",),
            1,
            1,
            1,
        )
    with pytest.raises(ValueError, match="cannot exceed"):
        ScoringInputEpoch(Strategy.TODAY, "afternoon", "test:today:epoch", (), 1, 2, 1)
