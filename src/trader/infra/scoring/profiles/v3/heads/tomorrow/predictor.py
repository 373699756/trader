"""Per-industry Ridge and LightGBM inference for the V3 Tomorrow head."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np

from trader.application.ports.model_scoring import ModelInput, ModelPrediction, ProfileEvidence
from trader.domain.recommendation.model_scoring import ExposureContract
from trader.domain.recommendation.model_scoring.profile_identity import ScoringProfileId
from trader.infra.scoring.profiles.v3.bundle_codec import V3IndustryModelArtifact, V3TomorrowBundleArtifact


class V3TomorrowPredictor:
    def __init__(self, artifact: V3TomorrowBundleArtifact, evidence: ProfileEvidence) -> None:
        self._artifact = artifact
        self._evidence = evidence
        self._models = {
            industry: (model, lgb.Booster(model_str=model.lightgbm_model)) for industry, model in artifact.industries
        }

    @property
    def profile_id(self) -> ScoringProfileId:
        return self._artifact.profile_id

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

    @property
    def profile_evidence(self) -> ProfileEvidence:
        return self._evidence

    def predict(self, inputs: tuple[ModelInput, ...]) -> tuple[ModelPrediction, ...]:
        predictions: list[ModelPrediction] = []
        for item in inputs:
            selected = self._models.get(item.industry)
            if selected is None:
                raise ValueError("Tomorrow V3 input industry is not covered")
            model, booster = selected
            predictions.append(self._predict_one(item, model, booster))
        return tuple(predictions)

    def _predict_one(
        self,
        item: ModelInput,
        model: V3IndustryModelArtifact,
        booster: lgb.Booster,
    ) -> ModelPrediction:
        matrix = np.asarray((item.alpha_features,), dtype=np.float64)
        if matrix.shape[1:] != (len(self.feature_ids),):
            raise ValueError("Tomorrow V3 input feature width does not match the packaged artifact")
        means = np.asarray(model.transformer_means, dtype=np.float64)
        scales = np.asarray(model.transformer_scales, dtype=np.float64)
        standardized = (matrix - means) / scales
        coefficients = np.asarray(model.ridge_coefficients, dtype=np.float64)
        ridge = float(model.ridge_intercept + standardized[0] @ coefficients)
        tree = float(
            booster.predict(
                standardized,
                num_iteration=model.lightgbm_best_iteration,
                num_threads=1,
            )[0]
        )
        combined = self._artifact.ridge_weight * ridge + self._artifact.lightgbm_weight * tree
        return ModelPrediction(
            item.code,
            model.calibration_intercept + model.calibration_slope * combined,
            abs(ridge - tree),
        )


__all__ = ["V3TomorrowPredictor"]
