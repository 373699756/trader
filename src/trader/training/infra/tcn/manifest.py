"""Feature manifest for a complete-session TCN sequence with auxiliary inputs."""

from trader.recommendation.domain.market.feature_contracts import FeatureId, FeatureVectorManifest
from trader.recommendation.domain.market.v5.features import SEQUENCE_CHANNELS, SEQUENCE_STEPS

SEQUENCE_FEATURE_IDS = tuple(
    FeatureId(f"sequence_{step}_{channel}") for step in range(SEQUENCE_STEPS) for channel in SEQUENCE_CHANNELS
)


def TcnFeatureManifest(auxiliary: FeatureVectorManifest) -> FeatureVectorManifest:
    return FeatureVectorManifest(
        (*auxiliary.feature_ids, *SEQUENCE_FEATURE_IDS),
        (*auxiliary.units, *(("ratio", "ratio", "ratio", "ratio", "log_shares", "log_yuan") * SEQUENCE_STEPS)),
        (*auxiliary.missing_policies, *((auxiliary.missing_policies[0],) * len(SEQUENCE_FEATURE_IDS))),
        auxiliary.catalog_hash,
    )
