"""Construct the configured immutable scoring profile at the composition boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from trader.infra.process_lock import ProcessLockError
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.pipeline.local_score.model_capability import PublishedModelScoringService
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import (
    ProductionModelScoringService,
    SharedModelFeatureCache,
)
from trader.recommendation.application.pipeline.local_score.unavailable_model import UnavailableModelCapability
from trader.recommendation.application.pipeline.local_score.v1.scoring import LearnedRankScoringService
from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.training.domain.model_publication import HeadTrainingStatus
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.model_bundles.profile_publication import (
    InspectActiveProfileBundles,
    ProfilePublicationBusyError,
)
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

ModelLoadFailure = Literal["missing", "corrupt", "integrity_conflict", "contract_conflict", "publication_busy"]


def BuildProfileScoring(
    profile_id: ScoringProfileId,
    loaded: LoadedScoringProfile | None,
    *,
    history_sessions: int,
    model_failure: str,
    cost_policy: ExecutionCostPolicy,
    maintenance: Callable[[], tuple[HeadTrainingStatus, ...]] | None,
) -> PublishedModelScoringService:
    """Construct only in-memory capabilities; bootstrap owns all I/O and external resources."""
    shared = SharedModelFeatureCache()
    return PublishedModelScoringService(
        ModelScoringRouter(
            profile_id,
            {
                strategy: LearnedRankScoringService(loaded, strategy, cost_policy=cost_policy, content_hash=ContentHash)
                if loaded is not None and profile_id == "v1"
                else ProductionModelScoringService(loaded, strategy, cost_policy=cost_policy, shared_features=shared)
                if loaded is not None
                else UnavailableModelCapability(profile_id, history_sessions, model_failure)
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            },
        ),
        maintenance=maintenance,
    )


class ScoringModelsUnavailableError(RuntimeError):
    """An expected artifact failure closes model admission without blocking Web."""

    def __init__(self, reason: ModelLoadFailure) -> None:
        self.reason = reason
        super().__init__(f"production_model_{reason}")


class ScoringModelsMissingError(ScoringModelsUnavailableError):
    """The selected profile has no complete published pair of strategy heads."""

    def __init__(self) -> None:
        super().__init__("missing")


def LoadScoringProfile(
    profile_id: ScoringProfileId,
    *,
    training_root: Path,
) -> LoadedScoringProfile:
    """Load one authorized profile without exposing artifact details to callers."""

    if profile_id == "v1":
        from trader.training.infra.profile.v1.artifacts import LoadV1ScoringProfile

        try:
            return LoadV1ScoringProfile(training_root)
        except FileNotFoundError as exc:
            raise ScoringModelsMissingError() from exc
        except ProcessLockError as exc:
            raise ScoringModelsUnavailableError("publication_busy") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise ScoringModelsUnavailableError("corrupt") from exc
    if profile_id in {"v2", "v3"}:
        try:
            profile = V2_TRAINING_PROFILE if profile_id == "v2" else V3_TRAINING_PROFILE
            inspected = InspectActiveProfileBundles(training_root / profile.output_directory, profile)
            bundles = tuple(item.model for item in inspected)
            return BuildTrainedScoringProfile(profile_id, bundles)
        except FileNotFoundError as exc:
            raise ScoringModelsMissingError() from exc
        except ProfilePublicationBusyError as exc:
            raise ScoringModelsUnavailableError("publication_busy") from exc
        except ModelBundleIntegrityError as exc:
            raise ScoringModelsUnavailableError("integrity_conflict") from exc
        except ModelBundleContractError as exc:
            raise ScoringModelsUnavailableError("contract_conflict") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise ScoringModelsUnavailableError("corrupt") from exc
    raise ValueError("unknown scoring profile")


__all__ = [
    "BuildProfileScoring",
    "ModelLoadFailure",
    "ScoringModelsMissingError",
    "ScoringModelsUnavailableError",
    "LoadScoringProfile",
]
