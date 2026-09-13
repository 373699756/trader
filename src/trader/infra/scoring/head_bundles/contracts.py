"""Immutable contracts for the three shared daily-close proxy heads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trader.domain.market.feature_contracts import (
    D25_MODEL_FEATURE_MANIFEST,
    TODAY_MODEL_FEATURE_MANIFEST,
    TOMORROW_MODEL_FEATURE_MANIFEST,
    FeatureVectorManifest,
)
from trader.domain.recommendation.models import Strategy

TrainedTargetColumn = Literal["target_t1", "target_d25_aggregate"]


@dataclass(frozen=True)
class TrainedHeadContract:
    strategy: Strategy
    model_id: str
    feature_manifest: FeatureVectorManifest
    feature_positions: tuple[int, ...]
    target_column: TrainedTargetColumn
    maturity_sessions: int
    runtime_anchor: Literal["11:20", "14:50"]
    label_target: Literal[
        "pre_cost_excess_return",
        "pre_cost_excess_return_t1",
        "pre_cost_mean_excess_return_t2_t5",
    ]

    @property
    def directory_name(self) -> str:
        return f"{self.strategy.value}-v3"


TODAY_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.TODAY,
    "today_industry_ridge_lightgbm",
    TODAY_MODEL_FEATURE_MANIFEST,
    (1, 2, 3, 4, 5),
    "target_t1",
    1,
    "11:20",
    "pre_cost_excess_return_t1",
)
TOMORROW_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.TOMORROW,
    "industry_ridge_lightgbm",
    TOMORROW_MODEL_FEATURE_MANIFEST,
    (0, 1, 2, 3, 4, 5),
    "target_t1",
    1,
    "14:50",
    "pre_cost_excess_return",
)
D25_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.D25,
    "d25_industry_ridge_lightgbm",
    D25_MODEL_FEATURE_MANIFEST,
    (1, 2, 3, 4, 5),
    "target_d25_aggregate",
    5,
    "14:50",
    "pre_cost_mean_excess_return_t2_t5",
)
HEAD_CONTRACTS = (TODAY_HEAD_CONTRACT, TOMORROW_HEAD_CONTRACT, D25_HEAD_CONTRACT)


def contract_for_strategy(strategy: Strategy) -> TrainedHeadContract:
    try:
        return next(item for item in HEAD_CONTRACTS if item.strategy is strategy)
    except StopIteration as exc:
        raise ValueError(f"{strategy.value} has no trained model head") from exc


__all__ = [
    "D25_HEAD_CONTRACT",
    "TODAY_HEAD_CONTRACT",
    "TOMORROW_HEAD_CONTRACT",
    "HEAD_CONTRACTS",
    "TrainedHeadContract",
    "TrainedTargetColumn",
    "contract_for_strategy",
]
