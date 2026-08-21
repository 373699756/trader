from __future__ import annotations

import pytest

from trader.domain.research.score_r6_daily import (
    SCORE_R6_DAILY_SPEC,
    ScoreR6DailyCandidate,
    iter_score_r6_daily_candidates,
)


def test_daily_trend_grid_is_fixed_unique_and_without_production_authority() -> None:
    candidates = iter_score_r6_daily_candidates(SCORE_R6_DAILY_SPEC)

    assert len(candidates) == 48
    assert len({candidate.content_hash for candidate in candidates}) == 48
    assert candidates == tuple(sorted(candidates, key=lambda candidate: candidate.content_hash))
    assert len(SCORE_R6_DAILY_SPEC.content_hash) == 64
    assert SCORE_R6_DAILY_SPEC.promotion_authority is False


def test_daily_trend_candidate_rejects_values_outside_preregistered_grid() -> None:
    with pytest.raises(ValueError, match="outside the preregistered grid"):
        ScoreR6DailyCandidate((3000, 2500, 2000, 1500, 1000), 71, 8.0, -12.0)
