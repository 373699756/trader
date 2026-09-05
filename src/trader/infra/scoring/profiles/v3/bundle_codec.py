"""Decode and validate a hash-bound V3 Tomorrow training bundle."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from trader.domain.recommendation.model_scoring import ExposureContract, V3_EXPOSURE_CONTRACT
from trader.infra.scoring.artifact_hashing import artifact_content_hash

_FEATURE_IDS = (
    "qfq_return_1d",
    "qfq_return_3d",
    "qfq_return_5d",
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
_FEATURE_UNITS = ("decimal_return",) * len(_FEATURE_IDS)
_MODEL_ID = "tomorrow_v3_industry_ridge_lightgbm"
_DOCUMENT_FIELDS = {
    "schema_version",
    "profile_id",
    "model_id",
    "strategy_head",
    "feature_ids",
    "feature_units",
    "exposure_contract",
    "manifest_hash",
    "split_hash",
    "report_hash",
    "training_anchor",
    "runtime_anchor",
    "point_in_time_parity",
    "training_rows",
    "validation_rows",
    "industry_count",
    "ensemble_weights",
    "industries",
    "dependencies",
    "automatic_model_update",
}
_INDUSTRY_MODEL_FIELDS = {
    "transformer_means",
    "transformer_scales",
    "ridge_intercept",
    "ridge_coefficients",
    "lightgbm_model",
    "lightgbm_best_iteration",
    "calibration_intercept",
    "calibration_slope",
    "training_rows",
    "validation_rows",
}


@dataclass(frozen=True)
class V3IndustryModelArtifact:
    transformer_means: tuple[float, ...]
    transformer_scales: tuple[float, ...]
    ridge_intercept: float
    ridge_coefficients: tuple[float, ...]
    lightgbm_model: str
    lightgbm_best_iteration: int
    calibration_intercept: float
    calibration_slope: float
    training_rows: int
    validation_rows: int


@dataclass(frozen=True)
class V3TomorrowBundleArtifact:
    profile_id: Literal["v3"]
    model_id: str
    feature_ids: tuple[str, ...]
    feature_units: tuple[str, ...]
    exposure_contract: ExposureContract
    ridge_weight: float
    lightgbm_weight: float
    manifest_hash: str
    split_hash: str
    report_hash: str
    training_rows: int
    validation_rows: int
    industries: tuple[tuple[str, V3IndustryModelArtifact], ...]
    dependencies: tuple[tuple[str, str], ...]
    content_hash: str


def load_v3_tomorrow_bundle(path: Path) -> V3TomorrowBundleArtifact:
    document = json.loads(path.read_text(encoding="utf-8"))
    return decode_v3_tomorrow_bundle(document)


def decode_v3_tomorrow_bundle(document: object) -> V3TomorrowBundleArtifact:
    if not isinstance(document, dict):
        raise TypeError("Tomorrow V3 training model must be a JSON object")
    payload = cast(dict[str, object], dict(document))
    stored_hash = payload.pop("content_hash", None)
    if not isinstance(stored_hash, str) or artifact_content_hash(payload) != stored_hash:
        raise ValueError("Tomorrow V3 training model content hash is invalid")
    missing_fields = _DOCUMENT_FIELDS - set(payload)
    if missing_fields & {"feature_ids", "feature_units"}:
        raise ValueError("Tomorrow V3 feature contract is incomplete")
    if "exposure_contract" in missing_fields:
        raise ValueError("Tomorrow V3 exposure contract is incomplete")
    if "ensemble_weights" in missing_fields:
        raise ValueError("Tomorrow V3 ensemble weights are incomplete")
    if set(payload) != _DOCUMENT_FIELDS:
        raise ValueError("Tomorrow V3 training model fields are invalid")
    feature_ids = tuple(_string_list(payload, "feature_ids"))
    feature_units = tuple(_string_list(payload, "feature_units"))
    exposure_contract = _exposure_contract(payload)
    ridge_weight, lightgbm_weight = _ensemble_weights(payload)
    if (
        _text(payload, "schema_version") != "tomorrow_v3_production_model_v1"
        or _text(payload, "profile_id") != "v3"
        or _text(payload, "model_id") != _MODEL_ID
        or _text(payload, "strategy_head") != "tomorrow"
        or feature_ids != _FEATURE_IDS
        or feature_units != _FEATURE_UNITS
        or exposure_contract != V3_EXPOSURE_CONTRACT
        or not _sha256_text(payload, "manifest_hash")
        or not _sha256_text(payload, "split_hash")
        or not _sha256_text(payload, "report_hash")
        or _text(payload, "training_anchor") != "15:00_close"
        or _text(payload, "runtime_anchor") != "14:50"
        or _boolean(payload, "point_in_time_parity")
        or _boolean(payload, "automatic_model_update")
        or _integer(payload, "training_rows") < 1
        or _integer(payload, "validation_rows") < 1
    ):
        raise ValueError("Tomorrow V3 training model identity or feature contract is invalid")
    dependencies = _dependencies(payload)
    raw_industries = payload.get("industries")
    if not isinstance(raw_industries, dict) or not raw_industries:
        raise ValueError("Tomorrow V3 industry models are missing")
    industries = tuple(
        sorted(
            (
                _industry_name(industry),
                _industry_model(raw, len(feature_ids)),
            )
            for industry, raw in raw_industries.items()
        )
    )
    if len({industry for industry, _model in industries}) != len(industries):
        raise ValueError("Tomorrow V3 industry names must be unique")
    if len(industries) != _integer(payload, "industry_count"):
        raise ValueError("Tomorrow V3 industry model count is invalid")
    return V3TomorrowBundleArtifact(
        "v3",
        _MODEL_ID,
        feature_ids,
        feature_units,
        exposure_contract,
        ridge_weight,
        lightgbm_weight,
        _text(payload, "manifest_hash"),
        _text(payload, "split_hash"),
        _text(payload, "report_hash"),
        _integer(payload, "training_rows"),
        _integer(payload, "validation_rows"),
        industries,
        dependencies,
        stored_hash,
    )


def _exposure_contract(payload: dict[str, object]) -> ExposureContract:
    raw = payload.get("exposure_contract")
    expected_keys = {"market", "board", "industry", "log_average_amount_20d", "order"}
    if not isinstance(raw, dict) or set(raw) != expected_keys:
        raise TypeError("Tomorrow V3 exposure contract is invalid")
    for name in expected_keys - {"order"}:
        if raw.get(name) is not True:
            raise ValueError("Tomorrow V3 exposure contract is invalid")
    order = raw.get("order")
    if not isinstance(order, list) or any(not isinstance(item, str) for item in order):
        raise TypeError("Tomorrow V3 exposure contract order is invalid")
    if tuple(order) != V3_EXPOSURE_CONTRACT.order:
        raise ValueError("Tomorrow V3 exposure contract is invalid")
    return V3_EXPOSURE_CONTRACT


def _ensemble_weights(payload: dict[str, object]) -> tuple[float, float]:
    raw = payload.get("ensemble_weights")
    if not isinstance(raw, dict) or set(raw) != {"ridge", "lightgbm"}:
        raise TypeError("Tomorrow V3 ensemble weights are invalid")
    ridge = _finite_number(raw.get("ridge"), "ridge ensemble weight")
    lightgbm = _finite_number(raw.get("lightgbm"), "LightGBM ensemble weight")
    if ridge != 0.5 or lightgbm != 0.5:
        raise ValueError("Tomorrow V3 ensemble weights are invalid")
    return ridge, lightgbm


def _industry_model(raw: object, width: int) -> V3IndustryModelArtifact:
    if not isinstance(raw, dict):
        raise TypeError("Tomorrow V3 industry model must be an object")
    values = cast(dict[str, object], raw)
    if set(values) != _INDUSTRY_MODEL_FIELDS:
        raise ValueError("Tomorrow V3 industry model fields are invalid")
    means = tuple(_number_list(values, "transformer_means"))
    scales = tuple(_number_list(values, "transformer_scales"))
    coefficients = tuple(_number_list(values, "ridge_coefficients"))
    numbers = (
        *means,
        *scales,
        _number(values, "ridge_intercept"),
        *coefficients,
        _number(values, "calibration_intercept"),
        _number(values, "calibration_slope"),
    )
    if (
        len(means) != width
        or len(scales) != width
        or len(coefficients) != width
        or any(not math.isfinite(item) for item in numbers)
        or any(item <= 0.0 for item in scales)
        or _integer(values, "lightgbm_best_iteration") < 1
        or _integer(values, "training_rows") < 1
        or _integer(values, "validation_rows") < 1
    ):
        raise ValueError("Tomorrow V3 industry model is invalid")
    return V3IndustryModelArtifact(
        means,
        scales,
        _number(values, "ridge_intercept"),
        coefficients,
        _text(values, "lightgbm_model"),
        _integer(values, "lightgbm_best_iteration"),
        _number(values, "calibration_intercept"),
        _number(values, "calibration_slope"),
        _integer(values, "training_rows"),
        _integer(values, "validation_rows"),
    )


def _dependencies(payload: dict[str, object]) -> tuple[tuple[str, str], ...]:
    raw = payload.get("dependencies")
    if (
        not isinstance(raw, dict)
        or set(raw) != {"lightgbm", "numpy"}
        or any(not isinstance(value, str) or not value for value in raw.values())
    ):
        raise ValueError("Tomorrow V3 dependency identity is invalid")
    return tuple(sorted(cast(dict[str, str], raw).items()))


def _industry_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError("Tomorrow V3 industry name must be non-empty text")
    return value.strip()


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Tomorrow V3 model {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Tomorrow V3 model {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    return _finite_number(payload.get(name), name)


def _finite_number(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        raise TypeError(f"Tomorrow V3 model {name} must be finite numeric")
    return float(value)


def _boolean(payload: dict[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"Tomorrow V3 model {name} must be boolean")
    return value


def _sha256_text(payload: dict[str, object], name: str) -> bool:
    value = _text(payload, name)
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _string_list(payload: dict[str, object], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"Tomorrow V3 model {name} must be a string list")
    return cast(list[str], value)


def _number_list(payload: dict[str, object], name: str) -> list[float]:
    value = payload.get(name)
    if not isinstance(value, list):
        raise TypeError(f"Tomorrow V3 model {name} must be a numeric list")
    return [_finite_number(item, name) for item in value]


__all__ = [
    "V3IndustryModelArtifact",
    "V3TomorrowBundleArtifact",
    "decode_v3_tomorrow_bundle",
    "load_v3_tomorrow_bundle",
]
