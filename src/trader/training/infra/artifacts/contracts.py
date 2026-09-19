"""Immutable contracts shared by profile-owned daily-close model heads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trader.recommendation.domain.market.feature_contracts import FeatureVectorManifest
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.publication.models import Strategy

TrainedTargetColumn = Literal["target_t1", "target_d25_aggregate"]


@dataclass(frozen=True)
class TrainedHeadContract:
    strategy: Strategy
    model_id: str
    feature_manifest: FeatureVectorManifest
    feature_positions: tuple[int, ...]
    target_column: TrainedTargetColumn
    maturity_sessions: int
    runtime_anchor: Literal["14:50"]
    label_target: Literal[
        "pre_cost_excess_return",
        "pre_cost_excess_return_t1",
        "pre_cost_mean_excess_return_t2_t5",
    ]

    @property
    def directory_name(self) -> str:
        return self.strategy.value


@dataclass(frozen=True)
class TrainedProfileContract:
    profile_id: ScoringProfileId
    output_directory: str
    history_sessions: int
    raw_feature_manifest: FeatureVectorManifest
    momentum_horizons: tuple[int, ...]
    market_state_momentum_horizons: tuple[int, ...]
    heads: tuple[TrainedHeadContract, ...]

    def __post_init__(self) -> None:
        if (
            self.profile_id not in {"v2", "v3"}
            or self.output_directory != self.profile_id
            or self.history_sessions < 61
            or self.history_sessions != max(self.momentum_horizons) + 1
            or len(set(self.momentum_horizons)) != len(self.momentum_horizons)
            or not set(self.market_state_momentum_horizons).issubset(self.momentum_horizons)
            or tuple(head.strategy for head in self.heads) != (Strategy.TOMORROW, Strategy.D25)
        ):
            raise ValueError("trained profile contract is invalid")

    def head_for_strategy(self, strategy: Strategy) -> TrainedHeadContract:
        try:
            return next(head for head in self.heads if head.strategy is strategy)
        except StopIteration as exc:
            raise ValueError(f"{strategy.value} has no trained model head") from exc


__all__ = [
    "TrainedHeadContract",
    "TrainedProfileContract",
    "TrainedTargetColumn",
]
