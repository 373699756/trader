from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.domain.research.score_r6 import (
    SCORE_R6_HISTORICAL_SPEC,
    iter_score_r6_candidates,
    materialize_score_r6_production_candidate,
)


def test_r6_candidate_grid_is_finite_shrunk_and_joint() -> None:
    assert SCORE_R6_HISTORICAL_SPEC.research_identity == "score_r6_historical_v2"
    assert SCORE_R6_HISTORICAL_SPEC.preregistered_on == date(2026, 9, 1)
    candidates = iter_score_r6_candidates(SCORE_R6_HISTORICAL_SPEC)

    assert candidates
    assert len({item.content_hash for item in candidates}) == len(candidates)
    assert {item.action_threshold for item in candidates} == {76, 78, 80}
    assert {item.risk_penalty for item in candidates} == {3, 4, 5}
    assert any(item.weight_units == (5000, 3000, 2000) for item in candidates)
    for candidate in candidates:
        assert sum(candidate.weight_units) == 10_000
        assert min(candidate.weight_units) >= 0
        assert (
            max(
                abs(value - current)
                for value, current in zip(
                    candidate.weight_units,
                    SCORE_R6_HISTORICAL_SPEC.current_weight_units,
                    strict=True,
                )
            )
            <= SCORE_R6_HISTORICAL_SPEC.maximum_component_offset_units
        )
        production = materialize_score_r6_production_candidate(candidate, SCORE_R6_HISTORICAL_SPEC)
        assert tuple(item.board for item in production.boards) == ("main", "chinext", "star")
        assert all(sum(item.weight_units) == 10_000 and min(item.weight_units) >= 0 for item in production.boards)


def test_r6_historical_spec_rejects_post_result_grid_changes() -> None:
    with pytest.raises(ValueError, match="threshold"):
        replace(SCORE_R6_HISTORICAL_SPEC, action_thresholds=(78, 79))
