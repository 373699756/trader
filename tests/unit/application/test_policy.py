from __future__ import annotations

from types import MappingProxyType

import pytest

from trader.application.policy import RecommendationPolicy, SelectionPolicy
from trader.domain.market.models import Board
from trader.domain.recommendation.fusion import FusionPolicy
from trader.domain.recommendation.models import Strategy


def test_selection_policy_default_competition_limits_are_isolated_and_immutable() -> None:
    first = _selection_policy()
    second = _selection_policy()

    assert first.competition_group_limits == {}
    assert second.competition_group_limits == {}
    assert first.competition_group_limits is not second.competition_group_limits
    with pytest.raises(TypeError):
        first.competition_group_limits[Board.MAIN] = 3  # type: ignore[index]


def test_recommendation_policy_default_board_weights_are_isolated_and_immutable() -> None:
    first = _recommendation_policy()
    second = _recommendation_policy()

    assert first.board_candidate_weights == {}
    assert first.board_local_strategy_weights == {}
    assert first.board_candidate_weights is not second.board_candidate_weights
    assert first.board_local_strategy_weights is not second.board_local_strategy_weights
    assert isinstance(first.board_candidate_weights, MappingProxyType)
    assert isinstance(first.board_local_strategy_weights, MappingProxyType)
    with pytest.raises(TypeError):
        first.board_candidate_weights[Strategy.TODAY] = {}  # type: ignore[index]


def _selection_policy() -> SelectionPolicy:
    return SelectionPolicy(
        default_top_k=10,
        maximum_top_k=18,
        maximum_per_industry=3,
        observation_margin=5.0,
        thresholds={"today_main": 70.0},
    )


def _recommendation_policy() -> RecommendationPolicy:
    return RecommendationPolicy(
        strategy_version="strategy-fixture",
        fusion_version="fusion-fixture",
        fusion=FusionPolicy(),
        selection=_selection_policy(),
        candidate_weights={},
        dimension_weights={},
        local_strategy_weights={},
        risk_rules={},
    )
