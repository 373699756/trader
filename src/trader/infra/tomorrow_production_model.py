"""Packaged LightGBM/ridge inference for the active Tomorrow P2 model."""

from __future__ import annotations

import json
from importlib import resources
from typing import cast

import lightgbm as lgb
import numpy as np

from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPrediction
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2ModelArtifact

_EXPECTED_MODEL_HASH = "27034e52813f1776e2ed218c1c397f481b244fb852b01be08ddc21249d887da5"
_RESOURCE_NAME = "tomorrow_p2_model.json"


class PackagedTomorrowProductionModel:
    def __init__(self, artifact: TomorrowHistoricalP2ModelArtifact) -> None:
        if artifact.content_hash != _EXPECTED_MODEL_HASH:
            raise ValueError("packaged Tomorrow production model hash is not authorized")
        self._artifact = artifact
        self._booster = lgb.Booster(model_str=artifact.lightgbm_model)

    @property
    def model_id(self) -> str:
        return self._artifact.candidate_id

    @property
    def model_hash(self) -> str:
        return self._artifact.content_hash

    def predict(self, inputs: tuple[TomorrowModelInput, ...]) -> tuple[TomorrowModelPrediction, ...]:
        if not inputs:
            return ()
        matrix = np.asarray(tuple(item.alpha_features for item in inputs), dtype=np.float64)
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


def load_packaged_tomorrow_production_model() -> PackagedTomorrowProductionModel:
    raw = json.loads(resources.files("trader.resources.models").joinpath(_RESOURCE_NAME).read_text(encoding="utf-8"))
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


__all__ = ["PackagedTomorrowProductionModel", "load_packaged_tomorrow_production_model"]
