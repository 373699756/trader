from __future__ import annotations

import pytest

from trader.domain.research.score_r6_stability import (
    SCORE_R6_STABILITY_SPEC,
    ScoreR6StabilityCandidate,
    iter_score_r6_stability_candidates,
)


def test_stability_grid_is_fixed_unique_and_without_production_authority() -> None:
    candidates = iter_score_r6_stability_candidates(SCORE_R6_STABILITY_SPEC)

    assert len(candidates) == 26
    assert len({candidate.content_hash for candidate in candidates}) == 26
    assert candidates == tuple(sorted(candidates, key=lambda candidate: candidate.content_hash))
    assert len(SCORE_R6_STABILITY_SPEC.content_hash) == 64
    assert SCORE_R6_STABILITY_SPEC.promotion_authority is False
    assert SCORE_R6_STABILITY_SPEC.evidence_class == "reused_observed_validation_window"


def test_stability_candidate_rejects_the_all_zero_control() -> None:
    with pytest.raises(ValueError, match="all-zero control"):
        ScoreR6StabilityCandidate(0.0, 0.0, 0.0)
