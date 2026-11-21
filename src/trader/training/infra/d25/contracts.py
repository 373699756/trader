"""Lookup of standard production D25 training contracts."""

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.d25.v2.contracts import V2_D25_PROFILE
from trader.training.infra.d25.v3.contracts import V3_D25_PROFILE
from trader.training.infra.d25.v5.contracts import V5_D25_PROFILE
from trader.training.infra.model_bundles.contracts import TrainedProfileContract


def StandardD25Profile(profile: ScoringProfileId) -> TrainedProfileContract:
    if profile == "v1":
        raise ValueError("v1 owns a separate D25 training contract")
    return {"v2": V2_D25_PROFILE, "v3": V3_D25_PROFILE, "v5": V5_D25_PROFILE}[profile]
