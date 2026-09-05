"""Construct the configured immutable scoring profile at the composition boundary."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import cast

from trader.application.ports.model_scoring import LoadedScoringProfile
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId
from trader.infra.scoring.profiles.v1.artifact_codec import decode_v1_tomorrow_artifact
from trader.infra.scoring.profiles.v1.profile import build_v1_scoring_profile
from trader.infra.scoring.profiles.v2.artifact_codec import decode_v2_tomorrow_artifact
from trader.infra.scoring.profiles.v2.profile import build_v2_scoring_profile
from trader.infra.scoring.profiles.v3.bundle_codec import load_v3_tomorrow_bundle
from trader.infra.scoring.profiles.v3.bundle_locator import locate_latest_v3_bundle
from trader.infra.scoring.profiles.v3.profile import build_v3_scoring_profile

_P2_RESOURCE_NAME = "tomorrow_p2_model.json"
_V1_RESOURCE_NAME = "tomorrow_v1_model.json"


def load_scoring_profile(
    profile_id: ScoringProfileId,
    *,
    training_root: Path | None = None,
) -> LoadedScoringProfile:
    """Load one authorized profile without exposing artifact details to callers."""

    if profile_id == "v1":
        v1_artifact = decode_v1_tomorrow_artifact(_resource_payload(_V1_RESOURCE_NAME))
        return build_v1_scoring_profile(v1_artifact)
    if profile_id == "v2":
        v2_artifact = decode_v2_tomorrow_artifact(_resource_payload(_P2_RESOURCE_NAME))
        return build_v2_scoring_profile(v2_artifact)
    if profile_id == "v3":
        try:
            bundle_path = locate_latest_v3_bundle(training_root or Path("data/train"))
            return build_v3_scoring_profile(load_v3_tomorrow_bundle(bundle_path))
        except FileNotFoundError as exc:
            raise RuntimeError("Tomorrow V3 training model is unavailable") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Tomorrow V3 training model is invalid") from exc
    raise ValueError("unknown scoring profile")


def _resource_payload(resource_name: str) -> dict[str, object]:
    raw = json.loads(resources.files("trader.resources.models").joinpath(resource_name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("packaged scoring model must be a JSON object")
    return cast(dict[str, object], raw)


__all__ = ["load_scoring_profile"]
