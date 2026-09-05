"""Pure domain contracts shared by scoring profiles and strategy heads."""

from trader.domain.recommendation.model_scoring.profile_identity import (
    SCORING_PROFILE_IDS,
    ScoringProfileId,
    parse_scoring_profile,
)
from trader.domain.recommendation.model_scoring.residualization import residualize_exposure
from trader.domain.recommendation.model_scoring.utility_scoring import percentile_ranks, positive_utility_scores

__all__ = [
    "SCORING_PROFILE_IDS",
    "ScoringProfileId",
    "parse_scoring_profile",
    "percentile_ranks",
    "positive_utility_scores",
    "residualize_exposure",
]
