"""Deterministic regularized linear control models for shadow research."""

from __future__ import annotations

import hashlib
import json

from trader.application.research.shadow_model_ports import ShadowFitRequest, ShadowFitResult, ShadowModelFamily
from trader.domain.research.shadow_calibration import fit_logistic_model, fit_ridge_model


class LinearShadowTrainer:
    @property
    def model_family(self) -> ShadowModelFamily:
        return "linear"

    def fit_predict(self, request: ShadowFitRequest) -> ShadowFitResult:
        model = (
            fit_ridge_model(request.train_x, request.train_y, ridge=1e-3)
            if request.objective == "net_excess"
            else fit_logistic_model(request.train_x, request.train_y, ridge=1e-3)
        )
        payload = {
            "family": self.model_family,
            "objective": request.objective,
            "feature_names": request.feature_names,
            "seed": request.seed,
            "ridge": 1e-3,
            "intercept": model.intercept,
            "coefficients": model.coefficients,
            "logistic": model.logistic,
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode()
        return ShadowFitResult(
            model_family="linear",
            model_hash=hashlib.sha256(encoded).hexdigest(),
            calibration_predictions=model.predict(request.calibration_x),
            prediction_predictions=model.predict(request.prediction_x),
        )


__all__ = ["LinearShadowTrainer"]
