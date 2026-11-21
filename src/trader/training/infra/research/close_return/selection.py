"""Explicit selection for diagnostic close-return heads."""

from enum import Enum

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.model_bundles.contracts import TrainedProfileContract
from trader.training.infra.research.close_return.v2_contracts import V2_TRAINING_PROFILE
from trader.training.infra.research.close_return.v3_contracts import V3_TRAINING_PROFILE
from trader.training.infra.research.close_return.v5_contracts import V5_TRAINING_PROFILE


class CloseReturnResearchMode(Enum):
    BOTH_HEADS = "both_close_return_heads"


def ResearchCloseReturnProfile(profile: ScoringProfileId) -> TrainedProfileContract:
    if profile == "v1":
        raise ValueError("v1 owns a separate learned-rank contract")
    return {"v2": V2_TRAINING_PROFILE, "v3": V3_TRAINING_PROFILE, "v5": V5_TRAINING_PROFILE}[profile]
