"""V3-only training feature, head, and output contracts."""

from trader.recommendation.domain.market.feature_contracts import (
    D25_MODEL_FEATURE_MANIFEST,
    TOMORROW_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract

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
D25_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.D25,
    "d25_industry_ridge",
    D25_MODEL_FEATURE_MANIFEST,
    (1, 2, 3, 4, 5),
    "target_d25_aggregate",
    5,
    "15:00",
    "pre_cost_mean_excess_return_t2_t5",
)
HEAD_CONTRACTS = (TOMORROW_HEAD_CONTRACT, D25_HEAD_CONTRACT)
_MOMENTUM_HORIZONS = (20, 40, 60)
V3_TRAINING_PROFILE = TrainedProfileContract(
    "v3",
    "v3",
    max(_MOMENTUM_HORIZONS) + 1,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    _MOMENTUM_HORIZONS,
    (),
    HEAD_CONTRACTS,
    "ridge",
)


def ContractForStrategy(strategy: Strategy) -> TrainedHeadContract:
    return V3_TRAINING_PROFILE.head_for_strategy(strategy)


__all__ = [
    "D25_HEAD_CONTRACT",
    "HEAD_CONTRACTS",
    "TOMORROW_HEAD_CONTRACT",
    "V3_TRAINING_PROFILE",
    "ContractForStrategy",
]
