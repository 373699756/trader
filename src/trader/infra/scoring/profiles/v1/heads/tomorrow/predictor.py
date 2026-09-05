"""Linear inference for the V1 Tomorrow strategy head."""

from __future__ import annotations

import numpy as np

from trader.application.ports.model_scoring import ProfileEvidence
from trader.application.ports.tomorrow_model import TomorrowModelInput, TomorrowModelPrediction, TomorrowScoringProfile
from trader.infra.scoring.profiles.v1.artifact_codec import V1TomorrowModelArtifact


class V1TomorrowPredictor:
    def __init__(self, artifact: V1TomorrowModelArtifact, evidence: ProfileEvidence) -> None:
        self._artifact = artifact
        self._evidence = evidence
        self._means = np.asarray(artifact.transformer_means, dtype=np.float64)
        self._scales = np.asarray(artifact.transformer_scales, dtype=np.float64)
        self._coefficients = np.asarray(artifact.linear_coefficients, dtype=np.float64)

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
            raise ValueError("Tomorrow V1 input feature width does not match the packaged artifact")
        predictions = (matrix - self._means) / self._scales @ self._coefficients + self._artifact.linear_intercept
        return tuple(
            TomorrowModelPrediction(item.code, float(prediction), 0.0)
            for item, prediction in zip(inputs, predictions, strict=True)
        )


__all__ = ["V1TomorrowPredictor"]
