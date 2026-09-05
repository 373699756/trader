"""Decode and validate the immutable V2 Tomorrow model artifact."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast

from trader.infra.scoring.artifact_hashing import artifact_content_hash

_AUTHORIZED_HASH = "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"


@dataclass(frozen=True)
class V2TomorrowModelArtifact:
    profile_id: Literal["v2"]
    model_id: str
    feature_ids: tuple[str, ...]
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    linear_intercept: float
    linear_coefficients: tuple[float, ...]
    lightgbm_model: str
    lightgbm_best_iteration: int
    training_rows: int
    internal_validation_rows: int
    content_hash: str


def decode_v2_tomorrow_artifact(document: object) -> V2TomorrowModelArtifact:
    if not isinstance(document, dict):
        raise TypeError("packaged Tomorrow V2 production model must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    stored_hash = payload.pop("content_hash", None)
    if not isinstance(stored_hash, str) or artifact_content_hash(payload) != stored_hash:
        raise ValueError("packaged Tomorrow V2 production model content hash is invalid")
    feature_ids = tuple(_string_list(payload, "feature_ids"))
    means = tuple(_number_list(payload, "transformer_means"))
    scales = tuple(_number_list(payload, "transformer_scales"))
    coefficients = tuple(_number_list(payload, "linear_coefficients"))
    width = len(feature_ids)
    model_id = _text(payload, "candidate_id")
    intercept = _number(payload, "linear_intercept")
    training_rows = _integer(payload, "training_rows")
    internal_validation_rows = _integer(payload, "internal_validation_rows")
    if (
        stored_hash != _AUTHORIZED_HASH
        or model_id != "daily_reconstructible_ensemble_v1"
        or width < 1
        or len(set(feature_ids)) != width
        or len(means) != width
        or len(scales) != width
        or len(coefficients) != width
        or not _text(payload, "lightgbm_model")
        or _integer(payload, "lightgbm_best_iteration") < 1
        or training_rows < 1
        or not 1 <= internal_validation_rows < training_rows
        or _text(payload, "schema_version") != "score_tomorrow_historical_p2_model_v1"
        or any(not math.isfinite(value) for value in (*means, *scales, intercept, *coefficients))
        or any(value <= 0.0 for value in scales)
    ):
        raise ValueError("packaged Tomorrow V2 production model identity is invalid")
    return V2TomorrowModelArtifact(
        "v2",
        model_id,
        feature_ids,
        means,
        scales,
        intercept,
        coefficients,
        _text(payload, "lightgbm_model"),
        _integer(payload, "lightgbm_best_iteration"),
        training_rows,
        internal_validation_rows,
        stored_hash,
    )


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Tomorrow V2 model {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Tomorrow V2 model {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"Tomorrow V2 model {name} must be numeric")
    return float(value)


def _string_list(payload: dict[str, object], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"Tomorrow V2 model {name} must be a string list")
    return cast(list[str], value)


def _number_list(payload: dict[str, object], name: str) -> list[float]:
    value = payload.get(name)
    if not isinstance(value, list) or any(
        not isinstance(item, (int, float)) or isinstance(item, bool) for item in value
    ):
        raise TypeError(f"Tomorrow V2 model {name} must be a numeric list")
    return [float(item) for item in value]


__all__ = ["V2TomorrowModelArtifact", "decode_v2_tomorrow_artifact"]
