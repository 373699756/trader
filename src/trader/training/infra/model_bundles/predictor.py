"""Deterministic inference for a shared trained strategy head."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import lightgbm as lgb
import numpy as np

from trader.recommendation.application.ports.loaded_profile import ModelInput, ModelPrediction
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.residualization import ExposureContract
from trader.training.infra.model_bundles.bundle_codec import (
    TrainedHeadModelBundle,
    TrainedLightGBMModelSnapshot,
    TrainedRidgeModelSnapshot,
    TrainedTcnModelSnapshot,
)

if TYPE_CHECKING:
    from trader.training.infra.profile.v5.network import TcnNetwork


_PREDICTION_BATCH_SIZE = 256


@dataclass(frozen=True)
class _IndustryArrays:
    means: np.ndarray
    scales: np.ndarray
    coefficients: np.ndarray | None


def _readonly_array(values: tuple[float, ...]) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    array.setflags(write=False)
    return array


def PredictRidgeBatch(matrix: np.ndarray, coefficients: np.ndarray, *, intercept: float) -> np.ndarray:
    """Use the same row reduction for calibration, diagnostics and strict utility gates."""
    if coefficients.ndim != 1 or matrix.shape[1:] != coefficients.shape:
        raise ValueError("Ridge batch matrix and coefficient dimensions are invalid")
    return np.fromiter(
        (float(intercept + row @ coefficients) for row in matrix),
        dtype=np.float64,
        count=len(matrix),
    )


class TrainedHeadPredictor:
    def __init__(
        self,
        profile_id: ScoringProfileId,
        bundle: TrainedHeadModelBundle,
        strategy: Strategy,
    ) -> None:
        if bundle.strategy is not strategy:
            raise ValueError("trained predictor strategy does not match its artifact")
        self._profile_id = profile_id
        self._bundle = bundle
        self._strategy = strategy
        self._models = dict(bundle.industries)
        self._feature_ids = tuple(name for name in bundle.feature_ids if not name.startswith("sequence_"))
        self._arrays = {
            industry: _IndustryArrays(
                _readonly_array(model.transformer_means),
                _readonly_array(model.transformer_scales),
                _readonly_array(model.ridge_coefficients) if isinstance(model, TrainedRidgeModelSnapshot) else None,
            )
            for industry, model in bundle.industries
        }
        self._boosters = {
            industry: lgb.Booster(model_str=model.lightgbm_model)
            for industry, model in bundle.industries
            if isinstance(model, TrainedLightGBMModelSnapshot)
        }
        self._tcn: dict[str, TcnNetwork] = {}
        if bundle.algorithm == "tcn":
            from trader.training.infra.profile.v5.network import TcnNetwork

            for industry, model in bundle.industries:
                if not isinstance(model, TrainedTcnModelSnapshot):
                    raise ValueError("V5 requires TCN industry models")
                network = TcnNetwork(len(bundle.feature_ids) - 360, model.dropout, model.architecture)
                network.load_tensors(model.weights)
                network.eval()
                self._tcn[industry] = network

    @property
    def profile_id(self) -> ScoringProfileId:
        return self._profile_id

    @property
    def model_id(self) -> str:
        return self._bundle.model_id

    @property
    def model_hash(self) -> str:
        return self._bundle.content_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self._feature_ids

    @property
    def exposure_contract(self) -> ExposureContract:
        return self._bundle.exposure_contract

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        self._require_inputs(inputs)
        if self._bundle.algorithm == "tcn":
            return self._predict_tcn(inputs)
        grouped: dict[str, list[ModelInput]] = {}
        for item in inputs:
            grouped.setdefault(item.industry, []).append(item)
        predicted_by_code: dict[str, float] = {}
        for industry, selected in grouped.items():
            model = self._models[industry]
            for start in range(0, len(selected), _PREDICTION_BATCH_SIZE):
                batch = selected[start : start + _PREDICTION_BATCH_SIZE]
                predicted = self._predict_industry_batch(industry, batch)
                for item, value in zip(batch, predicted, strict=True):
                    predicted_by_code[item.code] = model.calibration_intercept + model.calibration_slope * float(value)
        return tuple(ModelPrediction(item.code, predicted_by_code[item.code]) for item in inputs)

    def _require_inputs(self, inputs: Sequence[ModelInput]) -> None:
        if len({item.code for item in inputs}) != len(inputs):
            raise ValueError("trained prediction batch codes must be unique")
        if any(len(item.alpha_features) != len(self._feature_ids) for item in inputs):
            raise ValueError(f"{self._strategy.value} trained input feature width is invalid")
        if any(item.industry not in self._models for item in inputs):
            raise ValueError(f"{self._strategy.value} trained input industry is not covered")

    def _predict_industry_batch(self, industry: str, inputs: Sequence[ModelInput]) -> np.ndarray:
        model = self._models[industry]
        arrays = self._arrays[industry]
        matrix = np.asarray([item.alpha_features for item in inputs], dtype=np.float64)
        matrix -= arrays.means
        matrix /= arrays.scales
        if isinstance(model, TrainedLightGBMModelSnapshot):
            return np.asarray(
                self._boosters[industry].predict(matrix, num_iteration=model.lightgbm_best_iteration, num_threads=1),
                dtype=np.float64,
            )
        if isinstance(model, TrainedRidgeModelSnapshot):
            return self._predict_ridge_batch(industry, matrix)
        raise ValueError("TCN models require sequence batch inference")

    def _predict_tcn(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        predicted_by_code: dict[str, float] = {}
        if len({item.code for item in inputs}) != len(inputs):
            raise ValueError("V5 batch codes must be unique")
        for industry in sorted({item.industry for item in inputs}):
            model = self._models.get(industry)
            if not isinstance(model, TrainedTcnModelSnapshot):
                raise ValueError("V5 input industry is not covered")
            selected = tuple(item for item in inputs if item.industry == industry)
            for start in range(0, len(selected), 256):
                batch = selected[start : start + 256]
                if any(item.sequence is None or len(item.alpha_features) != len(self.feature_ids) for item in batch):
                    raise ValueError("V5 input is missing its sequence or auxiliary features")
                matrix = np.asarray(
                    [(*item.alpha_features, *item.sequence.values) for item in batch if item.sequence is not None],
                    dtype=np.float64,
                )
                matrix -= self._arrays[industry].means
                matrix /= self._arrays[industry].scales
                predicted = self._tcn[industry].predict(matrix)
                for item, value in zip(batch, predicted, strict=True):
                    predicted_by_code[item.code] = model.calibration_intercept + model.calibration_slope * float(value)
        return tuple(ModelPrediction(item.code, predicted_by_code[item.code]) for item in inputs)

    def _predict_ridge_batch(self, industry: str, matrix: np.ndarray) -> np.ndarray:
        model = self._models[industry]
        coefficients = self._arrays[industry].coefficients
        if not isinstance(model, TrainedRidgeModelSnapshot) or coefficients is None:
            raise ValueError("Ridge batch requires its own industry parameters")
        return PredictRidgeBatch(matrix, coefficients, intercept=model.ridge_intercept)


__all__ = ["PredictRidgeBatch", "TrainedHeadPredictor"]
