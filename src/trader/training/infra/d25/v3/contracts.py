"""V3 D25 production feature and utility artifact contracts."""

from trader.recommendation.domain.market.feature_contracts import (
    D25_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract

_MOMENTUM_HORIZONS = (20, 40, 60)

V3_D25_HEAD = TrainedHeadContract(
    Strategy.D25,
    "d25_industry_ridge",
    D25_MODEL_FEATURE_MANIFEST,
    tuple(range(len(D25_MODEL_FEATURE_MANIFEST.feature_ids))),
    "target_d25_aggregate",
    5,
    "15:00",
    "pre_cost_mean_excess_return_t2_t5",
)
V3_D25_PROFILE = TrainedProfileContract(
    "v3", "v3", max(_MOMENTUM_HORIZONS) + 1, TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    _MOMENTUM_HORIZONS, (), (V3_D25_HEAD,), "ridge",
)
