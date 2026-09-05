"""Hash-bound packaged inference for configured Tomorrow scoring profiles."""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from pathlib import Path
from typing import cast

import lightgbm as lgb
import numpy as np

from trader.application.ports.model_scoring import ProfileEvidence
from trader.application.ports.tomorrow_model import (
    TomorrowModelInput,
    TomorrowModelPrediction,
    TomorrowModelPredictorPort,
    TomorrowScoringProfile,
)
from trader.infra.scoring.profiles.v1.artifact_codec import decode_v1_tomorrow_artifact
from trader.infra.scoring.profiles.v1.profile import build_v1_tomorrow_predictor
from trader.infra.scoring.profiles.v2.artifact_codec import decode_v2_tomorrow_artifact
from trader.infra.scoring.profiles.v2.profile import build_v2_tomorrow_predictor

_P2_RESOURCE_NAME = "tomorrow_p2_model.json"
_V1_RESOURCE_NAME = "tomorrow_v1_model.json"


class PackagedV3TomorrowProductionModel:
    """Hash-bound per-industry Ridge/LightGBM inference for Tomorrow V3."""

    def __init__(self, payload: dict[str, object], stored_hash: str) -> None:
        if _content_hash(payload) != stored_hash:
            raise ValueError("packaged Tomorrow V3 production model hash is invalid")
        if (
            _text(payload, "profile_id") != "v3"
            or _text(payload, "schema_version") != "tomorrow_v3_production_model_v1"
            or not _text(payload, "model_id")
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
            raise ValueError("packaged Tomorrow V3 production model identity is invalid")
        self._model_id = _text(payload, "model_id")
        feature_ids = tuple(_string_list(payload, "feature_ids"))
        if feature_ids != (
            "qfq_return_1d",
            "qfq_return_3d",
            "qfq_return_5d",
            "qfq_residual_momentum_20d_skip5",
            "qfq_residual_momentum_40d_skip5",
            "qfq_residual_momentum_60d_skip5",
        ):
            raise ValueError("packaged Tomorrow V3 feature contract is invalid")
        self._feature_ids = feature_ids
        raw_industries = payload.get("industries")
        if not isinstance(raw_industries, dict) or not raw_industries:
            raise ValueError("packaged Tomorrow V3 industry models are missing")
        self._industries: dict[
            str, tuple[np.ndarray, np.ndarray, float, np.ndarray, lgb.Booster, int, float, float]
        ] = {}
        for industry, raw in raw_industries.items():
            if not isinstance(industry, str) or not isinstance(raw, dict):
                raise ValueError("packaged Tomorrow V3 industry model is invalid")
            means = np.asarray(_number_list(raw, "transformer_means"), dtype=np.float64)
            scales = np.asarray(_number_list(raw, "transformer_scales"), dtype=np.float64)
            coefficients = np.asarray(_number_list(raw, "ridge_coefficients"), dtype=np.float64)
            model_text = _text(raw, "lightgbm_model")
            if (
                len(means) != len(feature_ids)
                or len(scales) != len(feature_ids)
                or len(coefficients) != len(feature_ids)
            ):
                raise ValueError("packaged Tomorrow V3 industry feature width is invalid")
            if np.any(scales <= 0.0):
                raise ValueError("packaged Tomorrow V3 industry scale is invalid")
            self._industries[industry] = (
                means,
                scales,
                _number(raw, "ridge_intercept"),
                coefficients,
                lgb.Booster(model_str=model_text),
                _integer(raw, "lightgbm_best_iteration"),
                _number(raw, "calibration_intercept"),
                _number(raw, "calibration_slope"),
            )
        self._model_hash = stored_hash

    @property
    def profile_id(self) -> TomorrowScoringProfile:
        return "v3"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_hash(self) -> str:
        return self._model_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self._feature_ids

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._industries))

    @property
    def profile_evidence(self) -> ProfileEvidence:
        return ProfileEvidence("historical_validated", (), "trained_artifact")

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
        predictions: list[TomorrowModelPrediction] = []
        for item in inputs:
            model = self._industries.get(item.industry)
            if model is None:
                raise ValueError("Tomorrow V3 input industry is not covered")
            means, scales, intercept, coefficients, booster, iteration, calibration_intercept, calibration_slope = model
            matrix = np.asarray((item.alpha_features,), dtype=np.float64)
            if matrix.shape[1:] != (len(self._feature_ids),):
                raise ValueError("Tomorrow V3 input feature width does not match the packaged artifact")
            standardized = (matrix - means) / scales
            ridge = float(intercept + standardized[0] @ coefficients)
            tree = float(booster.predict(standardized, num_iteration=iteration, num_threads=1)[0])
            predictions.append(
                TomorrowModelPrediction(
                    item.code,
                    calibration_intercept + calibration_slope * (0.5 * ridge + 0.5 * tree),
                    abs(ridge - tree),
                )
            )
        return tuple(predictions)


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
            payload, stored_hash = _load_training_v3_model(training_root or Path("data/train"))
        except FileNotFoundError as exc:
            raise RuntimeError("Tomorrow V3 training model is unavailable") from exc
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError("Tomorrow V3 training model is invalid") from exc
        try:
            return PackagedV3TomorrowProductionModel(payload, stored_hash)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Tomorrow V3 training model is invalid") from exc
    raise ValueError("unknown Tomorrow scoring profile")


def _resource_payload(resource_name: str) -> dict[str, object]:
    raw = json.loads(resources.files("trader.resources.models").joinpath(resource_name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("packaged Tomorrow production model must be a JSON object")
    return cast(dict[str, object], raw)


def _load_training_v3_model(training_root: Path) -> tuple[dict[str, object], str]:
    model_root = training_root / "tomorrow-v3"
    candidates = tuple(path for path in model_root.glob("*/model.json") if path.is_file())
    if not candidates:
        raise FileNotFoundError(model_root / "*/model.json")
    model_path = max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))
    return _verified_json_document(model_path, "model")


def _verified_json_document(path: Path, label: str) -> tuple[dict[str, object], str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError(f"Tomorrow V3 training {label} must be a JSON object")
    payload = cast(dict[str, object], raw)
    stored_hash = payload.pop("content_hash", None)
    if not isinstance(stored_hash, str) or _content_hash(payload) != stored_hash:
        raise ValueError(f"Tomorrow V3 training {label} content hash is invalid")
    return payload, stored_hash


def _content_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _text(payload: dict[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise TypeError(f"Tomorrow model {name} must be non-empty text")
    return value


def _integer(payload: dict[str, object], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"Tomorrow model {name} must be an integer")
    return value


def _number(payload: dict[str, object], name: str) -> float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"Tomorrow model {name} must be numeric")
    return float(value)


def _boolean(payload: dict[str, object], name: str) -> bool:
    value = payload.get(name)
    if not isinstance(value, bool):
        raise TypeError(f"Tomorrow model {name} must be boolean")
    return value


def _sha256_text(payload: dict[str, object], name: str) -> bool:
    value = _text(payload, name)
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _string_list(payload: dict[str, object], name: str) -> list[str]:
    value = payload.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"Tomorrow model {name} must be a string list")
    return cast(list[str], value)


def _number_list(payload: dict[str, object], name: str) -> list[float]:
    value = payload.get(name)
    if not isinstance(value, list) or any(
        not isinstance(item, (int, float)) or isinstance(item, bool) for item in value
    ):
        raise TypeError(f"Tomorrow model {name} must be a numeric list")
    return [float(item) for item in value]


__all__ = [
    "PackagedV3TomorrowProductionModel",
    "load_packaged_tomorrow_production_model",
]
