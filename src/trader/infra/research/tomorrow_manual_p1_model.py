"""Bounded offline fitting for the manual Tomorrow P1 daily proxy artifact."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from trader.application.research.historical_screening import HistoricalArchiveManifest
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2Row
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC, HistoricalScreeningSpec

P1_MODEL_ID = "p1_manual_residual_momentum_v1"
P1_FEATURE_IDS: tuple[str, str, str] = (
    "qfq_residual_momentum_20d_skip5",
    "qfq_residual_momentum_40d_skip5",
    "qfq_residual_momentum_60d_skip5",
)
P1_FEATURE_CONTRACT = "h0_board_amount_residual_momentum_proxy_v1"
_RIDGE = 1e-3


@dataclass(frozen=True)
class TomorrowManualP1ModelArtifact:
    source_spec_hash: str
    source_manifest_hash: str
    training_start: date
    training_end: date
    transformer_means: tuple[float, float, float]
    transformer_scales: tuple[float, float, float]
    linear_intercept: float
    linear_coefficients: tuple[float, float, float]
    training_rows: int
    profile_id: str = "p1"
    model_id: str = P1_MODEL_ID
    feature_ids: tuple[str, str, str] = P1_FEATURE_IDS
    source_research_identity: str = "score_h0_v1"
    feature_contract: str = P1_FEATURE_CONTRACT
    schema_version: str = "tomorrow_production_linear_model_v1"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        values = (*self.transformer_means, *self.transformer_scales, self.linear_intercept, *self.linear_coefficients)
        if (
            self.profile_id != "p1"
            or self.model_id != P1_MODEL_ID
            or self.feature_ids != P1_FEATURE_IDS
            or self.source_research_identity != "score_h0_v1"
            or self.feature_contract != P1_FEATURE_CONTRACT
            or self.schema_version != "tomorrow_production_linear_model_v1"
            or len(self.source_spec_hash) != 64
            or len(self.source_manifest_hash) != 64
            or self.training_start > self.training_end
            or self.training_rows < 1
            or any(not math.isfinite(value) for value in values)
            or any(value <= 0.0 for value in self.transformer_scales)
        ):
            raise ValueError("manual Tomorrow P1 model artifact is invalid")
        object.__setattr__(self, "content_hash", _content_hash(production_artifact_payload(self)))


class _CompensatedSum:
    def __init__(self) -> None:
        self._total = 0.0
        self._correction = 0.0

    def add(self, value: float) -> None:
        adjusted = value - self._correction
        updated = self._total + adjusted
        self._correction = (updated - self._total) - adjusted
        self._total = updated

    @property
    def value(self) -> float:
        return self._total


def fit_manual_p1_model(
    rows: Iterable[TomorrowHistoricalP2Row],
    spec: HistoricalScreeningSpec,
    manifest: HistoricalArchiveManifest,
) -> TomorrowManualP1ModelArtifact:
    """Fit a deterministic ridge proxy without materializing the multi-million-row H0 archive."""

    if (
        spec != SCORE_H0_V1_SPEC
        or manifest.research_identity != spec.research_identity
        or manifest.spec_hash != spec.content_hash
    ):
        raise ValueError("manual Tomorrow P1 fitting requires the exact H0 manifest")

    sums = tuple(_CompensatedSum() for _ in range(3))
    cross = tuple(tuple(_CompensatedSum() for _ in range(3)) for _ in range(3))
    target_sum = _CompensatedSum()
    feature_target = tuple(_CompensatedSum() for _ in range(3))
    count = 0
    for row in rows:
        if not spec.training_start <= row.trade_date <= spec.training_end:
            continue
        features = row.alpha_features[3:]
        target = row.gross_excess_return
        count += 1
        target_sum.add(target)
        for left, value in enumerate(features):
            sums[left].add(value)
            feature_target[left].add(value * target)
            for right, other in enumerate(features):
                cross[left][right].add(value * other)
    if count < 5:
        raise ValueError("manual Tomorrow P1 fitting requires at least five H0 rows")
    means = tuple(item.value / count for item in sums)
    covariance = np.asarray(
        tuple(
            tuple(cross[left][right].value - count * means[left] * means[right] for right in range(3))
            for left in range(3)
        ),
        dtype=np.float64,
    )
    variances = tuple(max(0.0, float(covariance[index, index]) / count) for index in range(3))
    scales = tuple(math.sqrt(value) if value > 0.0 else 1.0 for value in variances)
    scale_array = np.asarray(scales, dtype=np.float64)
    standardized_cross = covariance / np.outer(scale_array, scale_array)
    centered_target = (
        np.asarray(
            tuple(feature_target[index].value - means[index] * target_sum.value for index in range(3)),
            dtype=np.float64,
        )
        / scale_array
    )
    coefficients = np.linalg.solve(standardized_cross + np.eye(3, dtype=np.float64) * _RIDGE, centered_target)
    return TomorrowManualP1ModelArtifact(
        source_spec_hash=spec.content_hash,
        source_manifest_hash=manifest.content_hash,
        training_start=spec.training_start,
        training_end=spec.training_end,
        transformer_means=(float(means[0]), float(means[1]), float(means[2])),
        transformer_scales=(float(scales[0]), float(scales[1]), float(scales[2])),
        linear_intercept=target_sum.value / count,
        linear_coefficients=(float(coefficients[0]), float(coefficients[1]), float(coefficients[2])),
        training_rows=count,
    )


def production_artifact_payload(artifact: TomorrowManualP1ModelArtifact) -> dict[str, object]:
    return {
        "feature_contract": artifact.feature_contract,
        "feature_ids": list(artifact.feature_ids),
        "linear_coefficients": list(artifact.linear_coefficients),
        "linear_intercept": artifact.linear_intercept,
        "model_id": artifact.model_id,
        "profile_id": artifact.profile_id,
        "schema_version": artifact.schema_version,
        "source_manifest_hash": artifact.source_manifest_hash,
        "source_research_identity": artifact.source_research_identity,
        "source_spec_hash": artifact.source_spec_hash,
        "training_end": artifact.training_end.isoformat(),
        "training_rows": artifact.training_rows,
        "training_start": artifact.training_start.isoformat(),
        "transformer_means": list(artifact.transformer_means),
        "transformer_scales": list(artifact.transformer_scales),
    }


def sealed_production_artifact_payload(artifact: TomorrowManualP1ModelArtifact) -> dict[str, object]:
    payload = production_artifact_payload(artifact)
    payload["content_hash"] = artifact.content_hash
    return payload


def _content_hash(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "P1_FEATURE_CONTRACT",
    "P1_FEATURE_IDS",
    "P1_MODEL_ID",
    "TomorrowManualP1ModelArtifact",
    "fit_manual_p1_model",
    "production_artifact_payload",
    "sealed_production_artifact_payload",
]
