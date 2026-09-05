"""Ridge and LightGBM ensemble inference for the V2 Tomorrow head."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np

from trader.application.ports.model_scoring import ProfileEvidence
from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPrediction, TomorrowScoringProfile
from trader.domain.recommendation.model_scoring import V1_V2_EXPOSURE_CONTRACT, ExposureContract
from trader.infra.scoring.profiles.v2.artifact_codec import V2TomorrowModelArtifact


class V2TomorrowPredictor:
    def __init__(self, artifact: V2TomorrowModelArtifact, evidence: ProfileEvidence) -> None:
        self._artifact = artifact
        self._evidence = evidence
        self._booster = lgb.Booster(model_str=artifact.lightgbm_model)

    @property
    def profile_id(self) -> TomorrowScoringProfile:
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
        return V1_V2_EXPOSURE_CONTRACT

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return ()

    @property
    def profile_evidence(self) -> ProfileEvidence:
        return self._evidence

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


__all__ = ["V2TomorrowPredictor"]
