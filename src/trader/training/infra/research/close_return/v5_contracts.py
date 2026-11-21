"""Independent V5 close-return diagnostic feature and artifact contracts."""

from dataclasses import replace

from trader.recommendation.domain.market.feature_contracts import TOMORROW_MODEL_FEATURE_MANIFEST
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.d25.v5.contracts import V5_D25_HEAD, V5_D25_PROFILE
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.tcn.manifest import TcnFeatureManifest

_TOMORROW_SEQUENCE_MANIFEST = TcnFeatureManifest(TOMORROW_MODEL_FEATURE_MANIFEST)
_TOMORROW = TrainedHeadContract(
    Strategy.TOMORROW,
    "v5_industry_tcn",
    _TOMORROW_SEQUENCE_MANIFEST,
    tuple(range(len(_TOMORROW_SEQUENCE_MANIFEST.feature_ids))),
    "target_t1",
    1,
    "15:00",
    "pre_cost_excess_return_t1",
)
_D25 = replace(V5_D25_HEAD, feature_positions=tuple(index + 1 for index in V5_D25_HEAD.feature_positions))
V5_TRAINING_PROFILE = replace(V5_D25_PROFILE, heads=(_TOMORROW, _D25))

__all__ = ["V5_TRAINING_PROFILE"]
