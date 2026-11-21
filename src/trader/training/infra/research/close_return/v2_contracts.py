"""Independent V2 close-return diagnostic feature and artifact contracts."""

from dataclasses import replace
from trader.recommendation.domain.market.feature_contracts import (
    V2_TOMORROW_MODEL_FEATURE_MANIFEST,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.d25.v2.contracts import V2_D25_HEAD, V2_D25_PROFILE
from trader.training.infra.model_bundles.contracts import TrainedHeadContract

V2_TOMORROW_HEAD_CONTRACT = TrainedHeadContract(
    Strategy.TOMORROW,
    "v2_industry_lightgbm",
    V2_TOMORROW_MODEL_FEATURE_MANIFEST,
    tuple(range(10)),
    "target_t1",
    1,
    "15:00",
    "pre_cost_excess_return_t1",
)
V2_D25_HEAD_CONTRACT = replace(V2_D25_HEAD, feature_positions=tuple(index + 1 for index in V2_D25_HEAD.feature_positions))
V2_HEAD_CONTRACTS = (V2_TOMORROW_HEAD_CONTRACT, V2_D25_HEAD_CONTRACT)
V2_TRAINING_PROFILE = replace(V2_D25_PROFILE, heads=V2_HEAD_CONTRACTS)

__all__ = [
    "V2_D25_HEAD_CONTRACT",
    "V2_HEAD_CONTRACTS",
    "V2_TOMORROW_HEAD_CONTRACT",
    "V2_TRAINING_PROFILE",
]
