"""Immutable model provenance and per-head maintenance observations."""

from dataclasses import dataclass
from datetime import date

from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.training_due import TrainingDueState


D25_PROBABILITY_TARGET_MISMATCH = "d25_probability_target_mismatch"


@dataclass(frozen=True)
class ModelPublicationIdentity:
    profile_id: ScoringProfileId
    strategy: Strategy
    model_hash: str
    metadata_hash: str
    label_cutoff: date
    training_history_hash: str
    training_run_hash: str
    probability_hash: str | None = None

    @property
    def probability_ready(self) -> bool:
        return self.probability_hash is not None


@dataclass(frozen=True)
class HeadTrainingStatus:
    profile_id: ScoringProfileId
    strategy: Strategy
    disk: ModelPublicationIdentity | None
    due: TrainingDueState | None
    failure: str | None = None
    loaded: ModelPublicationIdentity | None = None
    owned: bool = False
    publication_observation_failure: str | None = None

    @property
    def restart_required(self) -> bool:
        return (
            self.owned
            and self.loaded != self.disk
            and (self.failure != "publication_busy" or self.disk is not None)
            and (self.disk is not None or self.publication_observation_failure in {None, D25_PROBABILITY_TARGET_MISMATCH})
        )


__all__ = ["D25_PROBABILITY_TARGET_MISMATCH", "ModelPublicationIdentity", "HeadTrainingStatus"]
