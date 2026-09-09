from __future__ import annotations

import pytest

from trader.application.recommendation.candidate_planning import CandidatePlanSet
from trader.domain.market.models import Board
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.selection.scored_selection import (
    ScoredCandidatePlan,
    ScoredCandidateStageCounts,
)


def _empty_plan(strategy: Strategy, start: int) -> ScoredCandidatePlan:
    del strategy
    codes = iter(range(start, start + 360))
    reserves = {
        board: tuple(f"{next(codes):06d}" for _ in range(120)) for board in (Board.MAIN, Board.CHINEXT, Board.STAR)
    }
    return ScoredCandidatePlan(
        evaluations=(),
        reserves=reserves,
        population_versions={},
        stage_counts=ScoredCandidateStageCounts(0, 0, 0, 0, 0, 0),
        hard_filter_reason_counts={},
        population_rejected_count=0,
        population_filter_reason_counts={},
    )


def test_physical_candidate_union_is_bounded_by_three_strategy_board_windows() -> None:
    plans = CandidatePlanSet(
        {
            strategy: _empty_plan(strategy, offset)
            for strategy, offset in zip(
                (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25),
                (0, 360, 720),
                strict=True,
            )
        },
        limit_per_board=120,
    )

    assert len(plans.physical_union()) == 3 * 3 * 120
    assert all(len(plans.strategy_codes(strategy)) == 3 * 120 for strategy in plans.plans)


def test_candidate_plan_rejects_a_board_window_larger_than_the_fixed_cap() -> None:
    with pytest.raises(ValueError, match="between 1 and 120"):
        CandidatePlanSet(
            {
                strategy: _empty_plan(strategy, offset)
                for strategy, offset in zip(
                    (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25),
                    (0, 360, 720),
                    strict=True,
                )
            },
            limit_per_board=121,
        )


def test_candidate_plan_resolves_only_the_strategies_that_own_failed_codes() -> None:
    plans = CandidatePlanSet(
        {
            strategy: _empty_plan(strategy, offset)
            for strategy, offset in zip(
                (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25),
                (0, 360, 720),
                strict=True,
            )
        },
        limit_per_board=120,
    )

    assert plans.strategies_for_codes({"000000"}) == frozenset({Strategy.TODAY})
    assert plans.strategies_for_codes({"000360", "000720"}) == frozenset({Strategy.TOMORROW, Strategy.D25})
