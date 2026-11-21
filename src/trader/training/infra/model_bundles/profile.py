"""Assemble one runtime profile over the two scored strategy heads."""

from __future__ import annotations

from typing import cast

from lightgbm.basic import LightGBMError

from trader.recommendation.application.ports.loaded_profile import (
    HeadPredictorPort,
    HeadRuntime,
    LoadedScoringProfile,
    ProfileEvidence,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.model_publication import ModelPublicationIdentity
from trader.training.infra.model_bundles.bundle_codec import TrainedHeadModelBundle
from trader.training.infra.model_bundles.predictor import TrainedHeadPredictor
from trader.training.infra.profile.composition import SingleHeadCombiner


def BuildTrainedScoringProfile(
    profile_id: ScoringProfileId,
    bundles: tuple[TrainedHeadModelBundle, ...],
) -> LoadedScoringProfile:
    if profile_id not in {"v2", "v3", "v5"}:
        raise ValueError("shared trained heads require the V2, V3 or V5 scoring profile")
    by_strategy = {bundle.strategy: bundle for bundle in bundles}
    if len(bundles) != len(by_strategy) or set(by_strategy) not in ({Strategy.TOMORROW, Strategy.D25}, {Strategy.D25}):
        raise ValueError("trained scoring profile strategy bundles are invalid")
    if any(bundle.serialization_profile_id != profile_id for bundle in bundles):
        raise ValueError("shared scoring profile contains a foreign head")
    if any(bundle.final_fit is None or bundle.diagnostic_plan is None for bundle in bundles):
        raise ValueError("shared scoring profile requires current final-fit heads with recent production diagnostics")
    if (
        len({bundle.training_input_hash for bundle in bundles}) != 1
        or len({bundle.training_run_hash for bundle in bundles}) != 1
        or len({bundle.source_identity_hash for bundle in bundles}) != 1
    ):
        raise ValueError("shared scoring profile heads use different source snapshots or training runs")
    try:
        predictors: dict[Strategy, HeadPredictorPort] = {
            strategy: cast(HeadPredictorPort, TrainedHeadPredictor(profile_id, by_strategy[strategy], strategy))
            for strategy in by_strategy
        }
    except LightGBMError as exc:
        raise ValueError("shared LightGBM model is invalid") from exc
    return LoadedScoringProfile(
        profile_id=profile_id,
        heads={
            strategy: HeadRuntime(strategy, predictor, SingleHeadCombiner(), _evidence(by_strategy[strategy]))
            for strategy, predictor in predictors.items()
        },
        publications=tuple(
            ModelPublicationIdentity(
                profile_id,
                bundle.strategy,
                bundle.content_hash,
                bundle.metadata_hash,
                bundle.label_cutoff,
                bundle.training_input_hash,
                bundle.training_run_hash,
            )
            for bundle in bundles
        ),
    )


def _evidence(bundle: TrainedHeadModelBundle) -> ProfileEvidence:
    return ProfileEvidence(
        bundle.historical_status,
        bundle.historical_failure_reasons,
        "manual_user_override",
        training_anchor=bundle.training_anchor,
        runtime_anchor=bundle.runtime_anchor,
        point_in_time_parity=bundle.point_in_time_parity,
        production_authority=True,
    )


__all__ = ["BuildTrainedScoringProfile"]
