"""Legacy selector retained until consumers move to the scoring profile factory."""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import cast

from trader.application.ports.tomorrow_model import TomorrowModelPredictorPort, TomorrowScoringProfile
from trader.infra.scoring.profiles.v1.artifact_codec import decode_v1_tomorrow_artifact
from trader.infra.scoring.profiles.v1.profile import build_v1_tomorrow_predictor
from trader.infra.scoring.profiles.v2.artifact_codec import decode_v2_tomorrow_artifact
from trader.infra.scoring.profiles.v2.profile import build_v2_tomorrow_predictor
from trader.infra.scoring.profiles.v3.bundle_codec import load_v3_tomorrow_bundle
from trader.infra.scoring.profiles.v3.bundle_locator import locate_latest_v3_bundle
from trader.infra.scoring.profiles.v3.profile import build_v3_tomorrow_predictor

_P2_RESOURCE_NAME = "tomorrow_p2_model.json"
_V1_RESOURCE_NAME = "tomorrow_v1_model.json"


def load_packaged_tomorrow_production_model(
    profile_id: TomorrowScoringProfile,
    *,
    training_root: Path | None = None,
) -> TomorrowModelPredictorPort:
    if profile_id == "v1":
        return build_v1_tomorrow_predictor(decode_v1_tomorrow_artifact(_resource_payload(_V1_RESOURCE_NAME)))
    if profile_id == "v2":
        return build_v2_tomorrow_predictor(decode_v2_tomorrow_artifact(_resource_payload(_P2_RESOURCE_NAME)))
    if profile_id == "v3":
        try:
            path = locate_latest_v3_bundle(training_root or Path("data/train"))
        except FileNotFoundError as exc:
            raise RuntimeError("Tomorrow V3 training model is unavailable") from exc
        except OSError as exc:
            raise RuntimeError("Tomorrow V3 training model is invalid") from exc
        try:
            return build_v3_tomorrow_predictor(load_v3_tomorrow_bundle(path))
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Tomorrow V3 training model is invalid") from exc
    raise ValueError("unknown Tomorrow scoring profile")


def _resource_payload(resource_name: str) -> dict[str, object]:
    raw = json.loads(resources.files("trader.resources.models").joinpath(resource_name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("packaged Tomorrow production model must be a JSON object")
    return cast(dict[str, object], raw)


__all__ = ["load_packaged_tomorrow_production_model"]
