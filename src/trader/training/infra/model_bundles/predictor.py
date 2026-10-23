"""Deterministic inference for a shared trained strategy head."""

from __future__ import annotations

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
                network = TcnNetwork(len(bundle.feature_ids) - 360, model.dropout)
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
        return tuple(n for n in self._bundle.feature_ids if not n.startswith("sequence_"))

    @property
    def exposure_contract(self) -> ExposureContract:
        return self._bundle.exposure_contract

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        if self._bundle.algorithm == "tcn":
            return self._predict_tcn(inputs)
        predictions: list[ModelPrediction] = []
        for item in inputs:
            selected = self._models.get(item.industry)
            if selected is None:
                raise ValueError(f"{self._strategy.value} trained input industry is not covered")
            predictions.append(self._predict_one(item, selected))
        return tuple(predictions)

    def _predict_tcn(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
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
                matrix -= np.asarray(model.transformer_means)
                matrix /= np.asarray(model.transformer_scales)
                predicted = self._tcn[industry].predict(matrix)
                for item, value in zip(batch, predicted, strict=True):
                    predicted_by_code[item.code] = model.calibration_intercept + model.calibration_slope * float(value)
        return tuple(ModelPrediction(item.code, predicted_by_code[item.code]) for item in inputs)

    def _predict_one(
        self,
        item: ModelInput,
        model: TrainedRidgeModelSnapshot | TrainedLightGBMModelSnapshot | TrainedTcnModelSnapshot,
    ) -> ModelPrediction:
        matrix = np.asarray((item.alpha_features,), dtype=np.float64)
        if matrix.shape[1:] != (len(self.feature_ids),):
            raise ValueError(f"{self._strategy.value} trained input feature width is invalid")
        means = np.asarray(model.transformer_means, dtype=np.float64)
        scales = np.asarray(model.transformer_scales, dtype=np.float64)
        standardized = (matrix - means) / scales
        if isinstance(model, TrainedLightGBMModelSnapshot):
            booster = self._boosters[item.industry]
            predicted = float(
                booster.predict(standardized, num_iteration=model.lightgbm_best_iteration, num_threads=1)[0]
            )
        elif isinstance(model, TrainedRidgeModelSnapshot):
            coefficients = np.asarray(model.ridge_coefficients, dtype=np.float64)
            predicted = float(model.ridge_intercept + standardized[0] @ coefficients)
        else:
            raise ValueError("V5 requires sequence batch inference")
        return ModelPrediction(
            item.code,
            model.calibration_intercept + model.calibration_slope * predicted,
        )


__all__ = ["TrainedHeadPredictor"]
