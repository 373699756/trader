from __future__ import annotations

from types import MappingProxyType

import pytest

from trader.application.recommendation.policy import RecommendationPolicy, SelectionPolicy
from trader.domain.market.models import Board
from trader.domain.recommendation.models import Strategy
from trader.domain.recommendation.risk_fusion.fusion import FusionPolicy


def test_selection_policy_default_competition_limits_are_isolated_and_immutable() -> None:
    first = _selection_policy()
    second = _selection_policy()

    assert first.competition_group_limits == {}
    assert second.competition_group_limits == {}
    assert first.competition_group_limits is not second.competition_group_limits
    with pytest.raises(TypeError):
        first.competition_group_limits[Board.MAIN] = 3  # type: ignore[index]


def test_recommendation_policy_weight_maps_are_isolated_and_immutable() -> None:
    first = _recommendation_policy()
    second = _recommendation_policy()

    assert first.board_candidate_weights == {}
    assert first.board_local_strategy_weights == {}
    assert first.candidate_component_weights == {}
    assert first.local_component_weights == {}
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
        fusion=FusionPolicy(0.68, 0.32, 0.5, 2, 25.0, 30.0),
        selection=_selection_policy(),
        dimension_weights={},
        risk_rules={},
        board_policy_version="fixture",
        board_candidate_weights={},
        board_local_strategy_weights={},
        candidate_component_weights={},
        local_component_weights={},
    )
