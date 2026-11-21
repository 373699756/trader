"""V2 D25 production feature and utility artifact contracts."""

from trader.recommendation.domain.market.feature_contracts import (
    V2_D25_MODEL_FEATURE_MANIFEST,
    V2_RAW_ALPHA_FEATURE_MANIFEST,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract

_MOMENTUM_HORIZONS = (20, 40, 60, 120, 250)

V2_D25_HEAD = TrainedHeadContract(
    Strategy.D25,
    "v2_d25_industry_lightgbm",
    V2_D25_MODEL_FEATURE_MANIFEST,
    tuple(range(len(V2_D25_MODEL_FEATURE_MANIFEST.feature_ids))),
    "target_d25_aggregate",
    5,
    "15:00",
    "pre_cost_mean_excess_return_t2_t5",
)
V2_D25_PROFILE = TrainedProfileContract(
    "v2", "v2", max(_MOMENTUM_HORIZONS) + 1, V2_RAW_ALPHA_FEATURE_MANIFEST,
    _MOMENTUM_HORIZONS, (120, 250), (V2_D25_HEAD,), "lightgbm",
)
