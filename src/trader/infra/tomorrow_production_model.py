"""Hash-bound packaged inference for configured Tomorrow scoring profiles."""

from __future__ import annotations

import hashlib
import json
from importlib import resources
from typing import cast

import lightgbm as lgb
import numpy as np

from trader.application.ports.tomorrow_model import (
    TomorrowModelInput,
    TomorrowModelPrediction,
    TomorrowModelPredictorPort,
    TomorrowScoringProfile,
)
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2ModelArtifact

_EXPECTED_MODEL_HASH = "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"
_EXPECTED_V1_MODEL_HASH = "4291ea514c233a14ab6f9262e72ea541d1e9a794e73d02f10f8220509f6f502b"
_P2_RESOURCE_NAME = "tomorrow_p2_model.json"
_V1_RESOURCE_NAME = "tomorrow_v1_model.json"


class PackagedTomorrowProductionModel:
    def __init__(self, artifact: TomorrowHistoricalP2ModelArtifact) -> None:
        if artifact.content_hash != _EXPECTED_MODEL_HASH:
            raise ValueError("packaged Tomorrow production model hash is not authorized")
        self._artifact = artifact
        self._booster = lgb.Booster(model_str=artifact.lightgbm_model)

    @property
    def profile_id(self) -> TomorrowScoringProfile:
        return "v2"

    @property
    def model_id(self) -> str:
        return self._artifact.candidate_id

    @property
    def model_hash(self) -> str:
        return self._artifact.content_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self._artifact.feature_ids

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
        if not inputs:
            return ()
        matrix = np.asarray(tuple(item.alpha_features for item in inputs), dtype=np.float64)
        if matrix.shape[1:] != (len(self.feature_ids),):
            raise ValueError("Tomorrow V2 input feature width does not match the packaged artifact")
        means = np.asarray(self._artifact.transformer_means, dtype=np.float64)
        scales = np.asarray(self._artifact.transformer_scales, dtype=np.float64)
        standardized = (matrix - means) / scales
        coefficients = np.asarray(self._artifact.linear_coefficients, dtype=np.float64)
        linear = standardized @ coefficients + self._artifact.linear_intercept
        tree = np.asarray(
            self._booster.predict(
                standardized,
                num_iteration=self._artifact.lightgbm_best_iteration,
                num_threads=1,
            ),
            dtype=np.float64,
        ).reshape(-1)
        ensemble = 0.5 * linear + 0.5 * tree
        return tuple(
            TomorrowModelPrediction(
                item.code,
                float(prediction),
                abs(float(linear_value) - float(tree_value)),
            )
            for item, prediction, linear_value, tree_value in zip(
                inputs,
                ensemble,
                linear,
                tree,
                strict=True,
            )
        )


class PackagedLinearTomorrowProductionModel:
    def __init__(self, payload: dict[str, object], stored_hash: str) -> None:
        if _content_hash(payload) != stored_hash or stored_hash != _EXPECTED_V1_MODEL_HASH:
            raise ValueError("packaged Tomorrow V1 production model hash is not authorized")
        self._profile_id = _text(payload, "profile_id")
        self._model_id = _text(payload, "model_id")
        self._feature_ids = tuple(_string_list(payload, "feature_ids"))
        self._means = np.asarray(_number_list(payload, "transformer_means"), dtype=np.float64)
        self._scales = np.asarray(_number_list(payload, "transformer_scales"), dtype=np.float64)
        self._intercept = _number(payload, "linear_intercept")
        self._coefficients = np.asarray(_number_list(payload, "linear_coefficients"), dtype=np.float64)
        if (
            self._profile_id != "v1"
            or self._model_id != "v1_manual_residual_momentum_v1"
            or self._feature_ids
            != (
                "qfq_residual_momentum_20d_skip5",
                "qfq_residual_momentum_40d_skip5",
                "qfq_residual_momentum_60d_skip5",
            )
            or _text(payload, "schema_version") != "tomorrow_production_linear_model_v1"
            or _text(payload, "source_research_identity") != "score_h0_v1"
            or _text(payload, "feature_contract") != "h0_board_amount_residual_momentum_proxy_v1"
            or len(self._means) != len(self._feature_ids)
            or len(self._scales) != len(self._feature_ids)
            or len(self._coefficients) != len(self._feature_ids)
            or np.any(self._scales <= 0.0)
            or _integer(payload, "training_rows") < 1
            or len(_text(payload, "source_spec_hash")) != 64
            or len(_text(payload, "source_manifest_hash")) != 64
        ):
            raise ValueError("packaged Tomorrow V1 production model identity is invalid")
        self._model_hash = stored_hash

    @property
    def profile_id(self) -> TomorrowScoringProfile:
        return "v1"

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def model_hash(self) -> str:
        return self._model_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self._feature_ids

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
        if not inputs:
            return ()
        matrix = np.asarray(tuple(item.alpha_features for item in inputs), dtype=np.float64)
        if matrix.shape[1:] != (len(self._feature_ids),):
            raise ValueError("Tomorrow V1 input feature width does not match the packaged artifact")
        predictions = (matrix - self._means) / self._scales @ self._coefficients + self._intercept
        return tuple(
            TomorrowModelPrediction(item.code, float(prediction), 0.0)
            for item, prediction in zip(inputs, predictions, strict=True)
        )


def load_packaged_tomorrow_production_model(
    profile_id: TomorrowScoringProfile,
) -> TomorrowModelPredictorPort:
    if profile_id == "v1":
        raw = _resource_payload(_V1_RESOURCE_NAME)
        stored_hash = raw.pop("content_hash", None)
        if not isinstance(stored_hash, str):
            raise ValueError("packaged Tomorrow V1 production model hash is missing")
        return PackagedLinearTomorrowProductionModel(raw, stored_hash)
    if profile_id == "v2":
        raw = _resource_payload(_P2_RESOURCE_NAME)
        return _load_p2(raw)
    raise ValueError("unknown Tomorrow scoring profile")


def _load_p2(raw: dict[str, object]) -> PackagedTomorrowProductionModel:
    if not isinstance(raw, dict):
        raise TypeError("packaged Tomorrow production model must be a JSON object")
    payload = dict(raw)
    stored_hash = payload.pop("content_hash", None)
    if not isinstance(stored_hash, str):
        raise ValueError("packaged Tomorrow production model hash is missing")
    artifact = TomorrowHistoricalP2ModelArtifact(
        candidate_id=_text(payload, "candidate_id"),
        feature_ids=tuple(_string_list(payload, "feature_ids")),
        transformer_means=tuple(_number_list(payload, "transformer_means")),
        transformer_scales=tuple(_number_list(payload, "transformer_scales")),
        linear_intercept=_number(payload, "linear_intercept"),
        linear_coefficients=tuple(_number_list(payload, "linear_coefficients")),
        lightgbm_model=_text(payload, "lightgbm_model"),
        lightgbm_best_iteration=_integer(payload, "lightgbm_best_iteration"),
        training_rows=_integer(payload, "training_rows"),
        internal_validation_rows=_integer(payload, "internal_validation_rows"),
        schema_version=_text(payload, "schema_version"),
    )
    if artifact.content_hash != stored_hash:
        raise ValueError("packaged Tomorrow production model content hash is invalid")
    return PackagedTomorrowProductionModel(artifact)


def _resource_payload(resource_name: str) -> dict[str, object]:
    raw = json.loads(resources.files("trader.resources.models").joinpath(resource_name).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise TypeError("packaged Tomorrow production model must be a JSON object")
    return cast(dict[str, object], raw)


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
    "PackagedLinearTomorrowProductionModel",
    "PackagedTomorrowProductionModel",
    "load_packaged_tomorrow_production_model",
]
