"""Deterministic inference for a shared trained strategy head."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np

from trader.application.ports.model_scoring import ModelInput, ModelPrediction
from trader.recommendation.domain.scoring.residualization import ExposureContract
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.artifacts.bundle_codec import TrainedHeadBundleArtifact, TrainedIndustryModelArtifact


class TrainedHeadPredictor:
    def __init__(
        self,
        profile_id: ScoringProfileId,
        artifact: TrainedHeadBundleArtifact,
        strategy: Strategy,
    ) -> None:
        if artifact.strategy is not strategy:
            raise ValueError("trained predictor strategy does not match its artifact")
        self._profile_id = profile_id
        self._artifact = artifact
        self._strategy = strategy
        self._models = {
            industry: (model, lgb.Booster(model_str=model.lightgbm_model)) for industry, model in artifact.industries
        }

    @property
    def profile_id(self) -> ScoringProfileId:
        return self._profile_id

    @property
    def model_id(self) -> str:
        return self._artifact.model_id

    @property
    def model_hash(self) -> str:
        return self._artifact.content_hash

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self._artifact.feature_ids

    @property
    def exposure_contract(self) -> ExposureContract:
        return self._artifact.exposure_contract

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._models))

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        predictions: list[ModelPrediction] = []
        for item in inputs:
            selected = self._models.get(item.industry)
            if selected is None:
                raise ValueError(f"{self._strategy.value} trained input industry is not covered")
            predictions.append(self._predict_one(item, *selected))
        return tuple(predictions)

    def _predict_one(
        self,
        item: ModelInput,
        model: TrainedIndustryModelArtifact,
        booster: lgb.Booster,
    ) -> ModelPrediction:
        matrix = np.asarray((item.alpha_features,), dtype=np.float64)
        if matrix.shape[1:] != (len(self.feature_ids),):
            raise ValueError(f"{self._strategy.value} trained input feature width is invalid")
        means = np.asarray(model.transformer_means, dtype=np.float64)
        scales = np.asarray(model.transformer_scales, dtype=np.float64)
        standardized = (matrix - means) / scales
        coefficients = np.asarray(model.ridge_coefficients, dtype=np.float64)
        ridge = float(model.ridge_intercept + standardized[0] @ coefficients)
        tree = float(booster.predict(standardized, num_iteration=model.lightgbm_best_iteration, num_threads=1)[0])
        combined = self._artifact.ridge_weight * ridge + self._artifact.lightgbm_weight * tree
        return ModelPrediction(
            item.code,
            model.calibration_intercept + model.calibration_slope * combined,
            abs(ridge - tree),
        )


__all__ = ["TrainedHeadPredictor"]
