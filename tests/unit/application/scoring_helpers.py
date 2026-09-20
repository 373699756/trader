from __future__ import annotations

from trader.recommendation.application.ports.loaded_profile import (
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.infra.scoring.composition import SingleHeadCombiner


def profile_for(
    predictor,  # noqa: ANN001 - structural test double
    strategy: Strategy = Strategy.TOMORROW,
) -> LoadedScoringProfile:
    return LoadedScoringProfile(
        profile_id=predictor.profile_id,
        heads={
            strategy: HeadRuntime(
                strategy,
                predictor,
                SingleHeadCombiner(),
                ProfileEvidence("historical_unavailable", (), "manual_user_override"),
            )
        },
    )
