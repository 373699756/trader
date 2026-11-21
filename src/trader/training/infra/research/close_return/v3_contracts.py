"""Independent V3 close-return diagnostic feature and artifact contracts."""

from dataclasses import replace

from trader.recommendation.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.d25.v3.contracts import V3_D25_HEAD, V3_D25_PROFILE
from trader.training.infra.model_bundles.contracts import TrainedHeadContract

TOMORROW_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.TOMORROW,
    "industry_ridge",
    TOMORROW_MODEL_FEATURE_MANIFEST,
    (0, 1, 2, 3, 4, 5),
    "target_t1",
    1,
    "15:00",
    "pre_cost_excess_return",
)
D25_HEAD_CONTRACT = replace(V3_D25_HEAD, feature_positions=tuple(index + 1 for index in V3_D25_HEAD.feature_positions))
HEAD_CONTRACTS = (TOMORROW_HEAD_CONTRACT, D25_HEAD_CONTRACT)
V3_TRAINING_PROFILE = replace(V3_D25_PROFILE, heads=HEAD_CONTRACTS)


def ContractForStrategy(strategy: Strategy) -> TrainedHeadContract:
    return V3_TRAINING_PROFILE.head_for_strategy(strategy)


__all__ = [
    "D25_HEAD_CONTRACT",
    "HEAD_CONTRACTS",
    "TOMORROW_HEAD_CONTRACT",
    "V3_TRAINING_PROFILE",
    "ContractForStrategy",
]
