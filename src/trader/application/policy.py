"""Validated application policies independent from configuration transport."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from trader.domain.market.models import Board
from trader.domain.recommendation.filters import HardFilterPolicy
from trader.domain.recommendation.fusion import FusionPolicy
from trader.domain.recommendation.models import (
    BoardStrategyPolicy,
    Strategy,
)
from trader.domain.review.models import RiskRule


@dataclass(frozen=True)
class SelectionPolicy:
    default_top_k: int
    maximum_top_k: int
    maximum_per_industry: int
    observation_margin: float
    thresholds: Mapping[str, float]
    maximum_board_fraction: float = 1.0
    competition_group_limits: Mapping[Board, int] = field(default_factory=lambda: MappingProxyType({}))
    candidate_min_score: float = 0.0
    minimum_board_reliability: float = 0.0
    review_candidate_limit: int = 28

    def __post_init__(self) -> None:
        object.__setattr__(self, "thresholds", MappingProxyType(dict(self.thresholds)))
        object.__setattr__(self, "competition_group_limits", MappingProxyType(dict(self.competition_group_limits)))
        if not 0 <= self.default_top_k <= self.maximum_top_k <= 18:
            raise ValueError("TopK bounds must satisfy 0 <= default <= maximum <= 18")
        if self.maximum_per_industry < 1:
            raise ValueError("maximum_per_industry must be positive")
        if self.observation_margin < 0.0:
            raise ValueError("observation_margin cannot be negative")
        if not 0.0 < self.maximum_board_fraction <= 1.0:
            raise ValueError("maximum_board_fraction must be in (0, 1]")
        if any(board is Board.UNSUPPORTED or limit < 1 for board, limit in self.competition_group_limits.items()):
            raise ValueError("competition group limits must be positive for supported boards")
        if not 0.0 <= self.candidate_min_score <= 100.0:
            raise ValueError("candidate_min_score must be in [0, 100]")
        if not 0.0 <= self.minimum_board_reliability <= 1.0:
            raise ValueError("minimum_board_reliability must be in [0, 1]")
        if not 0 <= self.review_candidate_limit <= 120:
            raise ValueError("review_candidate_limit must be in [0, 120]")


@dataclass(frozen=True)
class RecommendationPolicy:
    strategy_version: str
    fusion_version: str
    fusion: FusionPolicy
    selection: SelectionPolicy
    candidate_weights: Mapping[str, float]
    dimension_weights: Mapping[Strategy, Mapping[str, float]]
    local_strategy_weights: Mapping[Strategy, Mapping[str, float]]
    risk_rules: Mapping[str, RiskRule]
    board_policy_version: str = ""
    board_candidate_weights: Mapping[Strategy, Mapping[Board, Mapping[str, float]]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    board_local_strategy_weights: Mapping[Strategy, Mapping[Board, Mapping[str, float]]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    hard_filter: HardFilterPolicy = field(default_factory=HardFilterPolicy)

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_weights", MappingProxyType(dict(self.candidate_weights)))
        object.__setattr__(
            self,
            "dimension_weights",
            MappingProxyType(
                {strategy: MappingProxyType(dict(weights)) for strategy, weights in self.dimension_weights.items()}
            ),
        )
        object.__setattr__(
            self,
            "local_strategy_weights",
            MappingProxyType(
                {strategy: MappingProxyType(dict(weights)) for strategy, weights in self.local_strategy_weights.items()}
            ),
        )
        object.__setattr__(self, "risk_rules", MappingProxyType(dict(self.risk_rules)))
        object.__setattr__(self, "board_candidate_weights", _freeze_board_weights(self.board_candidate_weights))
        object.__setattr__(
            self,
            "board_local_strategy_weights",
            _freeze_board_weights(self.board_local_strategy_weights),
        )

    def board_policy(self, strategy: Strategy, board: Board) -> BoardStrategyPolicy | None:
        candidate = self.board_candidate_weights.get(strategy, {}).get(board)
        local = self.board_local_strategy_weights.get(strategy, {}).get(board)
        if candidate is None or local is None:
            return None
        return BoardStrategyPolicy(
            policy_id=f"{self.board_policy_version}:{strategy.value}:{board.value}",
            version=self.board_policy_version,
            board=board,
            strategy=strategy,
            candidate_weights=candidate,
            local_weights=local,
            candidate_min_score=self.selection.candidate_min_score,
            minimum_reliability=self.selection.minimum_board_reliability,
        )


def _freeze_board_weights(
    values: Mapping[Strategy, Mapping[Board, Mapping[str, float]]],
) -> Mapping[Strategy, Mapping[Board, Mapping[str, float]]]:
    return MappingProxyType(
        {
            strategy: MappingProxyType({board: MappingProxyType(dict(weights)) for board, weights in boards.items()})
            for strategy, boards in values.items()
        }
    )


__all__ = ["RecommendationPolicy", "SelectionPolicy"]
