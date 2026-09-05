from __future__ import annotations

from trader.application.ports.model_scoring import (
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
    ProfileIdentity,
)
from trader.domain.recommendation.models import Strategy
from trader.infra.scoring.composition import SingleHeadCombiner


def profile_for(predictor) -> LoadedScoringProfile:  # noqa: ANN001 - structural test double
    return LoadedScoringProfile(
        identity=ProfileIdentity(predictor.profile_id, predictor.model_id, predictor.model_hash),
        heads=(HeadRuntime(Strategy.TOMORROW, predictor),),
        combiner=SingleHeadCombiner(),
        evidence=ProfileEvidence("historical_unavailable", (), "manual_user_override"),
    )
