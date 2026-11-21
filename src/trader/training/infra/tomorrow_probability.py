"""The current shared Tomorrow model and its atomic local publication."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import lightgbm as lgb
import numpy as np

from trader.infra.regular_file import ReadRegularFile
from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.sealing import ReplaceFile
from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy

_MAX_MODEL_BYTES = 16 * 1024 * 1024
_ARTIFACT_KEYS = {
    "target", "features", "history_snapshot_hash", "fitting_dates", "calibration_dates",
    "logistic", "lightgbm", "calibration",
}


@dataclass(frozen=True, slots=True)
class LogisticTomorrowModel:
    intercept: float
    weights: tuple[float, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]

    def __post_init__(self) -> None:
        width = len(TOMORROW_FEATURE_NAMES)
        if (
            len(self.weights) != width or len(self.means) != width or len(self.scales) != width
            or not all(math.isfinite(value) for value in (self.intercept, *self.weights, *self.means, *self.scales))
            or any(value <= 0 for value in self.scales)
        ):
            raise ValueError("tomorrow_logistic_model_invalid")


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityArtifact:
    target: PredictionTarget
    history_snapshot_hash: str
    fitting_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    calibration_intercept: float
    calibration_slope: float
    logistic: LogisticTomorrowModel | None
    lightgbm: str | None

    def __post_init__(self) -> None:
        if (
            self.target.strategy is not Strategy.TOMORROW
            or len(self.history_snapshot_hash) != 64
            or any(character not in "0123456789abcdef" for character in self.history_snapshot_hash)
            or not self.fitting_dates or not self.calibration_dates
            or self.fitting_dates != tuple(sorted(set(self.fitting_dates)))
            or self.calibration_dates != tuple(sorted(set(self.calibration_dates)))
            or self.fitting_dates[-1] >= self.calibration_dates[0]
            or not math.isfinite(self.calibration_intercept)
            or not math.isfinite(self.calibration_slope) or self.calibration_slope <= 0
            or (self.logistic is None) == (self.lightgbm is None)
            or self.lightgbm == ""
        ):
            raise ValueError("tomorrow_probability_artifact_invalid")

    @property
    def algorithm(self) -> str:
        return "logistic" if self.logistic is not None else "lightgbm"


@dataclass(frozen=True, slots=True)
class LoadedTomorrowProbabilityModel:
    identity: str
    artifact: TomorrowProbabilityArtifact
    booster: lgb.Booster | None

    def predict_probability(self, features: tuple[float, ...]) -> float:
        return self.predict_probabilities((features,))[0]

    def predict_probabilities(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        if not rows:
            return ()
        if any(
            len(features) != len(TOMORROW_FEATURE_NAMES) or not all(math.isfinite(value) for value in features)
            for features in rows
        ):
            raise ValueError("tomorrow_prediction_features_invalid")
        matrix = np.asarray(rows, dtype=np.float64)
        coefficients = self.artifact.logistic
        if coefficients is not None:
            scores = (
                (matrix - np.asarray(coefficients.means)) / np.asarray(coefficients.scales)
            ) @ np.asarray(coefficients.weights) + coefficients.intercept
        else:
            assert self.booster is not None
            raw = self.booster.predict(matrix, raw_score=True, num_threads=1)
            scores = np.asarray(raw, dtype=np.float64).reshape(-1)
        logits = self.artifact.calibration_intercept + self.artifact.calibration_slope * scores
        if len(logits) != len(rows) or not np.all(np.isfinite(logits)):
            raise ValueError("tomorrow_prediction_invalid")
        return tuple(float(value) for value in np.exp(-np.logaddexp(0.0, -logits)))


def _document(artifact: TomorrowProbabilityArtifact) -> dict[str, object]:
    logistic = artifact.logistic
    return {
        "target": artifact.target.identity,
        "features": TOMORROW_FEATURE_NAMES,
        "history_snapshot_hash": artifact.history_snapshot_hash,
        "fitting_dates": tuple(day.isoformat() for day in artifact.fitting_dates),
        "calibration_dates": tuple(day.isoformat() for day in artifact.calibration_dates),
        "logistic": None if logistic is None else {
            "intercept": logistic.intercept, "weights": logistic.weights,
            "means": logistic.means, "scales": logistic.scales,
        },
        "lightgbm": artifact.lightgbm,
        "calibration": (artifact.calibration_intercept, artifact.calibration_slope),
    }


def TomorrowProbabilityModelPath(model_root: Path, target: PredictionTarget) -> Path:
    if target.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow_probability_target_invalid")
    return model_root / f"tomorrow-{target.threshold_bps}.json"


def PublishTomorrowProbabilityModel(model_root: Path, artifact: TomorrowProbabilityArtifact) -> str:
    document = _document(artifact)
    identity = ContentHash(document)
    text = json.dumps({"identity": identity, "artifact": document}, ensure_ascii=True, allow_nan=False)
    if len(text) > _MAX_MODEL_BYTES:
        raise ValueError("tomorrow_probability_artifact_too_large")
    if artifact.lightgbm is not None:
        _load_tree(artifact.lightgbm)
    ReplaceFile(TomorrowProbabilityModelPath(model_root, artifact.target), text)
    return identity


def LoadTomorrowProbabilityModel(model_root: Path, target: PredictionTarget) -> LoadedTomorrowProbabilityModel | None:
    path = TomorrowProbabilityModelPath(model_root, target)
    try:
        encoded = ReadRegularFile(path, maximum_bytes=_MAX_MODEL_BYTES)
    except FileNotFoundError:
        return None
    package: object = json.loads(encoded)
    if not isinstance(package, dict) or set(package) != {"identity", "artifact"}:
        raise ValueError("tomorrow_probability_artifact_invalid")
    source: object = package["artifact"]
    identity: object = package["identity"]
    if not isinstance(source, dict) or set(source) != _ARTIFACT_KEYS or not isinstance(identity, str):
        raise ValueError("tomorrow_probability_artifact_invalid")
    if (
        ContentHash(source) != identity
        or source["target"] != target.identity
        or source["features"] != list(TOMORROW_FEATURE_NAMES)
    ):
        raise ValueError("tomorrow_probability_artifact_contract_mismatch")
    snapshot = source["history_snapshot_hash"]
    tree = source["lightgbm"]
    linear = source["logistic"]
    calibration = _numbers(source["calibration"])
    if (
        not isinstance(snapshot, str) or (tree is not None and not isinstance(tree, str)) or len(calibration) != 2
        or (linear is not None and (not isinstance(linear, dict) or set(linear) != {"intercept", "weights", "means", "scales"}))
    ):
        raise ValueError("tomorrow_probability_artifact_invalid")
    artifact = TomorrowProbabilityArtifact(
        target, snapshot, _dates(source["fitting_dates"]), _dates(source["calibration_dates"]), *calibration,
        logistic=None if linear is None else LogisticTomorrowModel(
            _number(linear["intercept"]), _numbers(linear["weights"]), _numbers(linear["means"]), _numbers(linear["scales"]),
        ),
        lightgbm=tree,
    )
    return LoadedTomorrowProbabilityModel(identity, artifact, None if tree is None else _load_tree(tree))


def _load_tree(tree: str) -> lgb.Booster:
    try:
        booster = lgb.Booster(model_str=tree)
    except lgb.basic.LightGBMError as exc:
        raise ValueError("tomorrow_probability_artifact_tree_invalid") from exc
    if (
        booster.num_trees() < 1 or tuple(booster.feature_name()) != TOMORROW_FEATURE_NAMES
        or not str(booster.dump_model().get("objective", "")).startswith("binary ")
    ):
        raise ValueError("tomorrow_probability_artifact_tree_invalid")
    return booster


def _number(value: object) -> float:
    if type(value) not in {int, float}:
        raise ValueError("tomorrow_probability_artifact_invalid")
    assert isinstance(value, (int, float))
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("tomorrow_probability_artifact_invalid") from exc
    if not math.isfinite(result):
        raise ValueError("tomorrow_probability_artifact_invalid")
    return result


def _numbers(values: object) -> tuple[float, ...]:
    if not isinstance(values, list):
        raise ValueError("tomorrow_probability_artifact_invalid")
    return tuple(_number(value) for value in values)


def _dates(values: object) -> tuple[date, ...]:
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("tomorrow_probability_artifact_invalid")
    return tuple(date.fromisoformat(value) for value in values)


__all__ = [
    "LoadedTomorrowProbabilityModel",
    "LoadTomorrowProbabilityModel",
    "LogisticTomorrowModel",
    "PublishTomorrowProbabilityModel",
    "TomorrowProbabilityArtifact",
    "TomorrowProbabilityModelPath",
]
