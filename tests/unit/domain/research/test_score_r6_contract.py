from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.research.score_r6 import (
    SCORE_R6_HISTORICAL_SPEC,
    ScoreR6ForwardSpec,
    iter_score_r6_candidates,
    materialize_score_r6_production_candidate,
)


def test_r6_candidate_grid_is_finite_shrunk_and_joint() -> None:
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


def test_r6_forward_spec_requires_new_exact_disjoint_future_window() -> None:
    registered_on = date(2026, 12, 1)
    planned = _weekdays_after(registered_on)
    spec = ScoreR6ForwardSpec(
        research_identity="score_r6_forward_20261201_v1",
        preregistered_on=registered_on,
        planned_trade_dates=planned,
        historical_report_hash="1" * 64,
        frozen_candidate_hash="2" * 64,
        trading_calendar_hash="3" * 64,
        rule_identity_hash="4" * 64,
        config_strategy_identity_hash="5" * 64,
    )

    assert spec.planned_trade_dates == planned
    assert spec.promotion_authority is False
    assert len(spec.content_hash) == 64

    with pytest.raises(ValueError, match="20 unique"):
        replace(spec, planned_trade_dates=planned[:-1])

    with pytest.raises(ValueError, match="after preregistration"):
        replace(spec, planned_trade_dates=(registered_on, *planned[1:]))


def _weekdays_after(start: date) -> tuple[date, ...]:
    result = []
    current = start
    while len(result) < 20:
        current += timedelta(days=1)
        if current.weekday() < 5:
            result.append(current)
    return tuple(result)
