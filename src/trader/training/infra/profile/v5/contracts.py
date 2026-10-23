"""V5 auxiliary and complete-session sequence feature identity."""

from trader.recommendation.domain.market.feature_contracts import (
    D25_MODEL_FEATURE_MANIFEST,
    TOMORROW_MODEL_FEATURE_MANIFEST,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    FeatureId,
    FeatureVectorManifest,
)
from trader.recommendation.domain.market.v5.features import SEQUENCE_CHANNELS, SEQUENCE_CONTRACT
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.contracts import TrainedHeadContract, TrainedProfileContract

SEQUENCE_FEATURE_IDS = tuple(
    FeatureId(f"sequence_{step}_{channel}") for step in range(60) for channel in SEQUENCE_CHANNELS
)


def _manifest(auxiliary: FeatureVectorManifest) -> FeatureVectorManifest:
    return FeatureVectorManifest(
        (*auxiliary.feature_ids, *SEQUENCE_FEATURE_IDS),
        (*auxiliary.units, *(("ratio", "ratio", "ratio", "ratio", "log_shares", "log_yuan") * 60)),
        (*auxiliary.missing_policies, *((auxiliary.missing_policies[0],) * 360)),
        auxiliary.catalog_hash,
    )


V5_TRAINING_PROFILE = TrainedProfileContract(
    "v5",
    "v5",
    62,
    TOMORROW_RAW_ALPHA_FEATURE_MANIFEST,
    (20, 40, 60),
    (),
    (
        TrainedHeadContract(
            Strategy.TOMORROW,
            "v5_industry_tcn",
            _manifest(TOMORROW_MODEL_FEATURE_MANIFEST),
            tuple(range(366)),
            "target_t1",
            1,
            "15:00",
            "pre_cost_excess_return_t1",
        ),
        TrainedHeadContract(
            Strategy.D25,
            "v5_d25_industry_tcn",
            _manifest(D25_MODEL_FEATURE_MANIFEST),
            tuple(range(1, 366)),
            "target_d25_aggregate",
            5,
            "15:00",
            "pre_cost_mean_excess_return_t2_t5",
        ),
    ),
    "tcn",
)

__all__ = ["V5_TRAINING_PROFILE", "SEQUENCE_FEATURE_IDS", "SEQUENCE_CONTRACT"]
