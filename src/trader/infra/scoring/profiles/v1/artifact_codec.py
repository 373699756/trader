"""Decode and validate the immutable V1 Tomorrow model artifact."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, cast

from trader.infra.scoring.artifact_hashing import artifact_content_hash

_AUTHORIZED_HASH = "4291ea514c233a14ab6f9262e72ea541d1e9a794e73d02f10f8220509f6f502b"
_FEATURE_IDS = (
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)


@dataclass(frozen=True)
class V1TomorrowModelArtifact:
    profile_id: Literal["v1"]
    model_id: str
    feature_ids: tuple[str, ...]
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    linear_intercept: float
    linear_coefficients: tuple[float, ...]
    content_hash: str


def decode_v1_tomorrow_artifact(document: object) -> V1TomorrowModelArtifact:
    if not isinstance(document, dict):
        raise TypeError("packaged Tomorrow V1 production model must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    stored_hash = payload.pop("content_hash", None)
    if (
        not isinstance(stored_hash, str)
        or artifact_content_hash(payload) != stored_hash
        or stored_hash != _AUTHORIZED_HASH
    ):
        raise ValueError("packaged Tomorrow V1 production model hash is not authorized")
    profile_id = _text(payload, "profile_id")
    model_id = _text(payload, "model_id")
    feature_ids = tuple(_string_list(payload, "feature_ids"))
    means = tuple(_number_list(payload, "transformer_means"))
    scales = tuple(_number_list(payload, "transformer_scales"))
    coefficients = tuple(_number_list(payload, "linear_coefficients"))
    intercept = _number(payload, "linear_intercept")
    if (
        profile_id != "v1"
        or model_id != "v1_manual_residual_momentum_v1"
        or feature_ids != _FEATURE_IDS
        or _text(payload, "schema_version") != "tomorrow_production_linear_model_v1"
        or _text(payload, "source_research_identity") != "score_h0_v1"
        or _text(payload, "feature_contract") != "h0_board_amount_residual_momentum_proxy_v1"
        or len(means) != len(feature_ids)
        or len(scales) != len(feature_ids)
        or len(coefficients) != len(feature_ids)
        or any(not math.isfinite(value) for value in (*means, *scales, intercept, *coefficients))
        or any(value <= 0.0 for value in scales)
        or _integer(payload, "training_rows") < 1
        or not _sha256_text(payload, "source_spec_hash")
        or not _sha256_text(payload, "source_manifest_hash")
    ):
        raise ValueError("packaged Tomorrow V1 production model identity is invalid")
    return V1TomorrowModelArtifact(
        "v1",
        model_id,
        feature_ids,
        means,
        scales,
        intercept,
        coefficients,
        stored_hash,
    )


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Tomorrow V1 model {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Tomorrow V1 model {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"Tomorrow V1 model {name} must be numeric")
    return float(value)


def _string_list(payload: dict[str, object], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"Tomorrow V1 model {name} must be a string list")
    return cast(list[str], value)


def _number_list(payload: dict[str, object], name: str) -> list[float]:
    value = payload.get(name)
    if not isinstance(value, list) or any(
        not isinstance(item, (int, float)) or isinstance(item, bool) for item in value
    ):
        raise TypeError(f"Tomorrow V1 model {name} must be a numeric list")
    return [float(item) for item in value]


def _sha256_text(payload: dict[str, object], name: str) -> bool:
    value = _text(payload, name)
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = ["V1TomorrowModelArtifact", "decode_v1_tomorrow_artifact"]
