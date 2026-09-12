"""Pure domain contracts shared by scoring profiles and strategy heads."""

from trader.domain.recommendation.model_scoring.profile_identity import (
    SCORING_PROFILE_IDS,
    ScoringProfileId,
    parse_scoring_profile,
)
from trader.domain.recommendation.model_scoring.residualization import (
    V1_V2_EXPOSURE_CONTRACT,
    V3_EXPOSURE_CONTRACT,
    ExposureContext,
    ExposureContract,
    ExposureDimension,
    create_exposure_context,
    residualize_exposure,
    residualize_exposure_with_context,
)
from trader.domain.recommendation.model_scoring.utility_scoring import percentile_ranks

__all__ = [
    "SCORING_PROFILE_IDS",
    "ExposureContract",
    "ExposureContext",
    "ExposureDimension",
    "ScoringProfileId",
    "V1_V2_EXPOSURE_CONTRACT",
    "V3_EXPOSURE_CONTRACT",
    "create_exposure_context",
    "parse_scoring_profile",
    "percentile_ranks",
    "residualize_exposure",
    "residualize_exposure_with_context",
]
