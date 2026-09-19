"""V2-only training feature, head, and output contracts."""

from trader.domain.market.feature_contracts import (
    V2_D25_MODEL_FEATURE_MANIFEST,
    V2_RAW_ALPHA_FEATURE_MANIFEST,
    V2_TODAY_MODEL_FEATURE_MANIFEST,
    V2_TOMORROW_MODEL_FEATURE_MANIFEST,
)
from trader.domain.recommendation.models import Strategy
from trader.training.infra.artifacts.contracts import TrainedHeadContract, TrainedProfileContract

V2_TODAY_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.TODAY,
    "v2_today_industry_ridge_lightgbm",
    V2_TODAY_MODEL_FEATURE_MANIFEST,
    tuple(range(1, 10)),
    "target_t1",
    1,
    "11:20",
    "pre_cost_excess_return_t1",
)
V2_TOMORROW_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.TOMORROW,
    "v2_industry_ridge_lightgbm",
    V2_TOMORROW_MODEL_FEATURE_MANIFEST,
    tuple(range(10)),
    "target_t1",
    1,
    "14:50",
    "pre_cost_excess_return_t1",
)
V2_D25_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.D25,
    "v2_d25_industry_ridge_lightgbm",
    V2_D25_MODEL_FEATURE_MANIFEST,
    tuple(range(1, 10)),
    "target_d25_aggregate",
    5,
    "14:50",
    "pre_cost_mean_excess_return_t2_t5",
)
V2_HEAD_CONTRACTS = (V2_TODAY_HEAD_CONTRACT, V2_TOMORROW_HEAD_CONTRACT, V2_D25_HEAD_CONTRACT)
V2_TRAINING_PROFILE = TrainedProfileContract(
    "v2",
    "v2",
    251,
    V2_RAW_ALPHA_FEATURE_MANIFEST,
    (20, 40, 60, 120, 250),
    (120, 250),
    V2_HEAD_CONTRACTS,
)

__all__ = [
    "V2_D25_HEAD_CONTRACT",
    "V2_HEAD_CONTRACTS",
    "V2_TODAY_HEAD_CONTRACT",
    "V2_TOMORROW_HEAD_CONTRACT",
    "V2_TRAINING_PROFILE",
]
