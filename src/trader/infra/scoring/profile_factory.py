"""Construct the configured immutable scoring profile at the composition boundary."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import cast

from trader.application.ports.model_scoring import LoadedScoringProfile
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId
from trader.infra.scoring.profiles.v1.artifact_codec import decode_tomorrow_artifact as decode_v1_artifact
from trader.infra.scoring.profiles.v1.profile import build_scoring_profile as build_v1_profile
from trader.infra.scoring.profiles.v2.artifact_codec import decode_tomorrow_artifact as decode_v2_artifact
from trader.infra.scoring.profiles.v2.profile import build_scoring_profile as build_v2_profile
from trader.infra.scoring.profiles.v3.bundle_codec import load_tomorrow_bundle as load_v3_bundle
from trader.infra.scoring.profiles.v3.bundle_locator import locate_latest_bundle as locate_latest_v3_bundle
from trader.infra.scoring.profiles.v3.profile import build_scoring_profile as build_v3_profile


def load_scoring_profile(
    profile_id: ScoringProfileId,
    *,
    training_root: Path | None = None,
) -> LoadedScoringProfile:
    """Load one authorized profile without exposing artifact details to callers."""

    if profile_id == "v1":
        v1_artifact = decode_v1_artifact(_profile_resource_payload("v1"))
        return build_v1_profile(v1_artifact)
    if profile_id == "v2":
        v2_artifact = decode_v2_artifact(_profile_resource_payload("v2"))
        return build_v2_profile(v2_artifact)
    if profile_id == "v3":
        try:
            bundle_path = locate_latest_v3_bundle(training_root or Path("data/train"))
            return build_v3_profile(load_v3_bundle(bundle_path))
        except FileNotFoundError as exc:
            raise RuntimeError("Tomorrow V3 training model is unavailable") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Tomorrow V3 training model is invalid") from exc
    raise ValueError("unknown scoring profile")


def _profile_resource_payload(profile_id: ScoringProfileId) -> dict[str, object]:
    package = f"trader.infra.scoring.profiles.{profile_id}"
    raw = json.loads(resources.files(package).joinpath("model.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("packaged scoring model must be a JSON object")
    return cast(dict[str, object], raw)


__all__ = ["load_scoring_profile"]
