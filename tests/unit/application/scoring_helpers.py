from __future__ import annotations

from trader.application.ports.model_scoring import (
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.composition import SingleHeadCombiner


def profile_for(predictor) -> LoadedScoringProfile:  # noqa: ANN001 - structural test double
    return LoadedScoringProfile(
        profile_id=predictor.profile_id,
        heads={
            Strategy.TOMORROW: HeadRuntime(
                Strategy.TOMORROW,
                predictor,
                SingleHeadCombiner(),
                ProfileEvidence("historical_unavailable", (), "manual_user_override"),
            )
        },
    )
