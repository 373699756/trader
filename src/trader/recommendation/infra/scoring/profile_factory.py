"""Construct the configured immutable scoring profile at the composition boundary."""

from __future__ import annotations

from pathlib import Path

from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.model_bundles.profile_publication import InspectActiveProfileBundles
from trader.training.infra.profile.v2.contracts import V2_TRAINING_PROFILE
from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE


class ScoringArtifactsMissingError(RuntimeError):
    """The selected profile has no complete published pair of strategy heads."""


def LoadScoringProfile(
    profile_id: ScoringProfileId,
    *,
    training_root: Path | None = None,
) -> LoadedScoringProfile:
    """Load one authorized profile without exposing artifact details to callers."""

    if profile_id in {"v2", "v3"}:
        try:
            profile = V2_TRAINING_PROFILE if profile_id == "v2" else V3_TRAINING_PROFILE
            inspected = InspectActiveProfileBundles(
                (training_root or Path("data/train")) / profile.output_directory, profile
            )
            bundles = tuple(item.model for item in inspected)
            return BuildTrainedScoringProfile(profile_id, bundles)
        except FileNotFoundError as exc:
            raise ScoringArtifactsMissingError("shared strategy-head training models are unavailable") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("shared strategy-head training models are invalid") from exc
    raise ValueError("unknown scoring profile")


__all__ = ["ScoringArtifactsMissingError", "LoadScoringProfile"]
