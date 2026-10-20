"""Immutable model provenance and per-head maintenance observations."""

from dataclasses import dataclass
from datetime import date

from trader.download.domain.history_control import HistoryTrainingDueState
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True)
class ModelPublicationIdentity:
    profile_id: ScoringProfileId
    strategy: Strategy
    model_hash: str
    metadata_hash: str
    label_cutoff: date
    training_history_hash: str
    training_run_hash: str


@dataclass(frozen=True)
class HeadTrainingStatus:
    profile_id: ScoringProfileId
    strategy: Strategy
    disk: ModelPublicationIdentity | None
    due: HistoryTrainingDueState | None
    failure: str | None = None
    loaded: ModelPublicationIdentity | None = None
    owned: bool = False

    @property
    def restart_required(self) -> bool:
        return self.owned and self.loaded != self.disk and self.failure != "publication_busy"


__all__ = ["ModelPublicationIdentity", "HeadTrainingStatus"]
