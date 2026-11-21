"""V5 D25 production feature and utility artifact contracts."""

from trader.recommendation.domain.market.feature_contracts import (
    D25_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract
from trader.training.infra.tcn.manifest import TcnFeatureManifest

_MOMENTUM_HORIZONS = (20, 40, 60)
_D25_SEQUENCE_MANIFEST = TcnFeatureManifest(D25_MODEL_FEATURE_MANIFEST)

V5_D25_HEAD = TrainedHeadContract(
    Strategy.D25,
    "v5_d25_industry_tcn",
    _D25_SEQUENCE_MANIFEST,
    tuple(range(len(_D25_SEQUENCE_MANIFEST.feature_ids))),
    "target_d25_aggregate",
    5,
    "15:00",
    "pre_cost_mean_excess_return_t2_t5",
)
V5_D25_PROFILE = TrainedProfileContract(
    "v5", "v5", max(_MOMENTUM_HORIZONS) + 1, TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    _MOMENTUM_HORIZONS, (), (V5_D25_HEAD,), "tcn",
)
