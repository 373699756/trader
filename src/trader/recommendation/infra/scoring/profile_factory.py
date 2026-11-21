"""Construct the configured immutable scoring profile at the composition boundary."""

from __future__ import annotations

from collections.abc import Callable
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.application.d25.v1.scoring import LearnedRankScoringService
from trader.recommendation.application.d25.v2.plan import V2_D25_PLAN
from trader.recommendation.application.d25.v3.plan import V3_D25_PLAN
from trader.recommendation.application.d25.v5.plan import V5_D25_PLAN
from trader.recommendation.application.pipeline.local_score.model_capability import PublishedModelScoringService
from trader.recommendation.application.pipeline.local_score.model_router import ModelScoringRouter
from trader.recommendation.application.pipeline.local_score.model_scoring import (
    ProductionModelScoringService,
    SharedModelFeatureCache,
)
from trader.recommendation.application.pipeline.local_score.unavailable_model import UnavailableModelCapability
from trader.recommendation.application.pipeline.local_score.tomorrow_probability import TomorrowProbabilityCapability
from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile, ScoringCapabilityPort
from trader.recommendation.application.tomorrow.v1.plan import V1_TOMORROW_PLAN
from trader.recommendation.application.tomorrow.v2.plan import V2_TOMORROW_PLAN
from trader.recommendation.application.tomorrow.v3.plan import V3_TOMORROW_PLAN
from trader.recommendation.application.tomorrow.v5.plan import V5_TOMORROW_PLAN
from trader.recommendation.domain.market.tomorrow_probability import TomorrowProbabilityPredictor
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.selection.execution_cost import ExecutionCostPolicy
from trader.training.domain.model_publication import HeadTrainingStatus
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, ModelBundleIntegrityError
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.model_bundles.profile_publication import (
    InspectPublishedProfileBundles,
    ProfilePublicationBusyError,
)

ModelLoadFailure = Literal[
    "missing", "corrupt", "integrity_conflict", "contract_conflict", "publication_busy", "dependency_missing"
]

_TOMORROW_PLANS = {
    "v1": V1_TOMORROW_PLAN, "v2": V2_TOMORROW_PLAN, "v3": V3_TOMORROW_PLAN, "v5": V5_TOMORROW_PLAN,
}
_D25_CROSS_SECTIONAL_PLANS = {"v2": V2_D25_PLAN, "v3": V3_D25_PLAN, "v5": V5_D25_PLAN}


def BuildProfileScoring(
    profile_id: ScoringProfileId,
    loaded: LoadedScoringProfile | None,
    *,
    history_sessions: int,
    model_failure: str,
    cost_policy: ExecutionCostPolicy,
    maintenance: Callable[[], tuple[HeadTrainingStatus, ...]] | None,
    tomorrow_target: PredictionTarget,
    d25_target: PredictionTarget,
    tomorrow_predictor: TomorrowProbabilityPredictor | None = None,
) -> PublishedModelScoringService:
    """Construct two independent in-memory capabilities for the configured profile."""
    if d25_target.strategy is not Strategy.D25:
        raise ValueError("D25 scoring requires a D25 prediction target")
    if loaded is not None and set(loaded.heads) != {Strategy.D25}:
        raise ValueError("production scoring requires a D25-only utility artifact")
    return PublishedModelScoringService(
        ModelScoringRouter(profile_id, {
            Strategy.TOMORROW: TomorrowProbabilityCapability(_TOMORROW_PLANS[profile_id], tomorrow_predictor, tomorrow_target),
            Strategy.D25: _utility_capability(
                profile_id, loaded, Strategy.D25, cost_policy, SharedModelFeatureCache(),
                history_sessions, model_failure, d25_target,
            ),
        }),
        maintenance=maintenance,
    )


def BuildDiagnosticProfileScoring(
    profile_id: ScoringProfileId,
    loaded: LoadedScoringProfile | None,
    *,
    history_sessions: int,
    model_failure: str,
    cost_policy: ExecutionCostPolicy,
    maintenance: Callable[[], tuple[HeadTrainingStatus, ...]] | None,
) -> PublishedModelScoringService:
    """Score explicit, in-memory utility-only diagnostic inputs without target claims."""
    shared = SharedModelFeatureCache()
    return PublishedModelScoringService(
        ModelScoringRouter(
            profile_id,
            {
                strategy: _utility_capability(
                    profile_id, loaded, strategy, cost_policy, shared, history_sessions, model_failure, None,
                )
                for strategy in (Strategy.TOMORROW, Strategy.D25)
            },
        ),
        maintenance=maintenance,
    )


def _utility_capability(
    profile: ScoringProfileId,
    loaded: LoadedScoringProfile | None,
    strategy: Strategy,
    cost_policy: ExecutionCostPolicy,
    shared: SharedModelFeatureCache,
    history_sessions: int,
    failure: str,
    target: PredictionTarget | None,
) -> ScoringCapabilityPort:
    if loaded is None or strategy not in loaded.heads:
        return UnavailableModelCapability(profile, history_sessions, failure, target)
    if profile == "v1":
        return LearnedRankScoringService(
            loaded, strategy, cost_policy=cost_policy, content_hash=ContentHash, prediction_target=target,
        )
    return ProductionModelScoringService(
        loaded, strategy, plan=_D25_CROSS_SECTIONAL_PLANS[profile], cost_policy=cost_policy,
        shared_features=shared, prediction_target=target,
    )


class ScoringModelsUnavailableError(RuntimeError):
    """An expected model failure closes admission without blocking Web."""

    def __init__(self, reason: ModelLoadFailure) -> None:
        self.reason = reason
        super().__init__(f"production_model_{reason}")


class ScoringModelsMissingError(ScoringModelsUnavailableError):
    """The selected profile has no published D25 utility artifact."""

    def __init__(self) -> None:
        super().__init__("missing")


def LoadD25UtilityProfile(
    profile_id: ScoringProfileId,
    *,
    training_root: Path,
) -> LoadedScoringProfile:
    """Read the independent D25 utility artifact for the requested profile."""
    try:
        if profile_id == "v1":
            from trader.training.infra.d25.v1.models import LoadV1ScoringProfile

            return LoadV1ScoringProfile(training_root, strategies=(Strategy.D25,))
        if profile_id == "v5":
            if find_spec("torch") is None:
                raise ScoringModelsUnavailableError("dependency_missing")
            from trader.training.infra.tcn.network import ConfigureTcnRuntime

            ConfigureTcnRuntime(training=False)
        profile = StandardD25Profile(profile_id)
        inspected = InspectPublishedProfileBundles(training_root / profile.output_directory, profile)
        return BuildTrainedScoringProfile(profile_id, tuple(item.model for item in inspected))
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


__all__ = [
    "BuildDiagnosticProfileScoring",
    "BuildProfileScoring",
    "LoadD25UtilityProfile",
    "ModelLoadFailure",
    "ScoringModelsMissingError",
    "ScoringModelsUnavailableError",
]
