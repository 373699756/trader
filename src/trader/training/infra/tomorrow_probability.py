"""Profile-owned Tomorrow models and atomic publication."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING

import lightgbm as lgb
import numpy as np

from trader.infra.regular_file import ReadRegularFile
from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.sealing import ReplaceFile
from trader.recommendation.domain.market.tomorrow_probability import TOMORROW_FEATURE_NAMES, TOMORROW_SEQUENCE_FEATURE_NAMES
from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.infra.profile.v5.settings import TCN_NETWORK_CONTRACT, TcnArchitecture
from trader.training.infra.profile.v5.tensors import DecodeTcnNetwork, TcnTensor
from trader.training.infra.tomorrow_settings import LoadTomorrowFitSettings, TomorrowAlgorithm

if TYPE_CHECKING:
    from trader.training.infra.profile.v5.network import TcnNetwork

_MAX_MODEL_BYTES = 16 * 1024 * 1024
_ARTIFACT_KEYS = {
    "profile", "algorithm", "target", "features", "history_snapshot_hash", "training_contract_hash",
    "fitting_dates", "calibration_dates", "logistic", "lightgbm", "tcn", "calibration",
}


def TomorrowFeatureNames(profile: ScoringProfileId) -> tuple[str, ...]:
    return TOMORROW_SEQUENCE_FEATURE_NAMES if profile == "v5" else TOMORROW_FEATURE_NAMES


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
class TomorrowTcnModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    dropout: float
    tensors: tuple[TcnTensor, ...]
    architecture: TcnArchitecture

    def __post_init__(self) -> None:
        width = len(TomorrowFeatureNames("v5"))
        if (
            len(self.means) != width or len(self.scales) != width
            or not all(math.isfinite(value) for value in (*self.means, *self.scales))
            or any(value <= 0 for value in self.scales) or not 0 <= self.dropout < 1
        ):
            raise ValueError("tomorrow_tcn_transforms_invalid")


@dataclass(frozen=True, slots=True)
class TomorrowProbabilityArtifact:
    profile: ScoringProfileId
    target: PredictionTarget
    history_snapshot_hash: str
    training_contract_hash: str
    fitting_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    calibration_intercept: float
    calibration_slope: float
    logistic: LogisticTomorrowModel | None
    lightgbm: str | None
    tcn: TomorrowTcnModel | None = None

    def __post_init__(self) -> None:
        if (
            self.target.strategy is not Strategy.TOMORROW
            or not _valid_hash(self.history_snapshot_hash) or not _valid_hash(self.training_contract_hash)
            or not self.fitting_dates or not self.calibration_dates
            or self.fitting_dates != tuple(sorted(set(self.fitting_dates)))
            or self.calibration_dates != tuple(sorted(set(self.calibration_dates)))
            or self.fitting_dates[-1] >= self.calibration_dates[0]
            or not math.isfinite(self.calibration_intercept)
            or not math.isfinite(self.calibration_slope) or self.calibration_slope <= 0
            or (self.logistic is not None) != (self.profile == "v3")
            or (self.lightgbm is not None) != (self.profile in {"v1", "v2"})
            or (self.tcn is not None) != (self.profile == "v5")
            or self.lightgbm == ""
        ):
            raise ValueError("tomorrow_probability_artifact_invalid")

    @property
    def algorithm(self) -> str:
        return TomorrowAlgorithm(self.profile)


@dataclass(frozen=True, slots=True)
class LoadedTomorrowProbabilityModel:
    identity: str
    artifact: TomorrowProbabilityArtifact
    booster: lgb.Booster | None
    network: TcnNetwork | None = None

    def predict_probability(self, features: tuple[float, ...]) -> float:
        return self.predict_probabilities((features,))[0]

    def predict_probabilities(self, rows: tuple[tuple[float, ...], ...]) -> tuple[float, ...]:
        if not rows:
            return ()
        width = len(TomorrowFeatureNames(self.artifact.profile))
        if any(
            len(features) != width or not all(math.isfinite(value) for value in features)
            for features in rows
        ):
            raise ValueError("tomorrow_prediction_features_invalid")
        matrix = np.asarray(rows, dtype=np.float64)
        coefficients = self.artifact.logistic
        if coefficients is not None:
            scores = (
                (matrix - np.asarray(coefficients.means)) / np.asarray(coefficients.scales)
            ) @ np.asarray(coefficients.weights) + coefficients.intercept
        elif self.network is not None:
            transforms = self.artifact.tcn
            assert transforms is not None
            means, scales = np.asarray(transforms.means), np.asarray(transforms.scales)
            scores = self.network.predict((matrix - means) / scales)
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
    tcn = artifact.tcn
    return {
        "profile": artifact.profile,
        "algorithm": artifact.algorithm,
        "target": artifact.target.identity,
        "features": TomorrowFeatureNames(artifact.profile),
        "history_snapshot_hash": artifact.history_snapshot_hash,
        "training_contract_hash": artifact.training_contract_hash,
        "fitting_dates": tuple(day.isoformat() for day in artifact.fitting_dates),
        "calibration_dates": tuple(day.isoformat() for day in artifact.calibration_dates),
        "logistic": None if logistic is None else {
            "intercept": logistic.intercept, "weights": logistic.weights,
            "means": logistic.means, "scales": logistic.scales,
        },
        "lightgbm": artifact.lightgbm,
        "tcn": None if tcn is None else {
            "means": tcn.means, "scales": tcn.scales,
            "network": {
                "contract": SEQUENCE_CONTRACT, "architecture": TCN_NETWORK_CONTRACT,
                "hidden_channels": tcn.architecture.hidden_channels, "compute_dtype": tcn.architecture.compute_dtype,
                "dropout": tcn.dropout, "weights": tuple(asdict(tensor) for tensor in tcn.tensors),
            },
        },
        "calibration": (artifact.calibration_intercept, artifact.calibration_slope),
    }


def TomorrowProbabilityModelPath(model_root: Path, profile: ScoringProfileId, target: PredictionTarget) -> Path:
    if target.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow_probability_target_invalid")
    return model_root / "tomorrow-targets" / profile / f"{target.threshold_bps}.json"


def PublishTomorrowProbabilityModel(model_root: Path, artifact: TomorrowProbabilityArtifact) -> str:
    document = _document(artifact)
    identity = ContentHash(document)
    text = json.dumps({"identity": identity, "artifact": document}, ensure_ascii=True, allow_nan=False)
    if len(text) > _MAX_MODEL_BYTES:
        raise ValueError("tomorrow_probability_artifact_too_large")
    if artifact.lightgbm is not None:
        _load_tree(artifact.lightgbm, artifact.profile)
    if artifact.tcn is not None:
        _decode_tcn(json.loads(text)["artifact"]["tcn"])
    ReplaceFile(TomorrowProbabilityModelPath(model_root, artifact.profile, artifact.target), text)
    return identity


def LoadTomorrowProbabilityModel(
    model_root: Path, profile: ScoringProfileId, target: PredictionTarget, *, project_root: Path,
) -> LoadedTomorrowProbabilityModel | None:
    path = TomorrowProbabilityModelPath(model_root, profile, target)
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
        or source["profile"] != profile or source["algorithm"] != TomorrowAlgorithm(profile)
        or source["target"] != target.identity
        or source["features"] != list(TomorrowFeatureNames(profile))
    ):
        raise ValueError("tomorrow_probability_artifact_contract_mismatch")
    training_identity = LoadTomorrowFitSettings(project_root, profile).identity
    if training_identity != source["training_contract_hash"]:
        raise ValueError("tomorrow_probability_training_contract_mismatch")
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
        profile, target, snapshot, training_identity, _dates(source["fitting_dates"]), _dates(source["calibration_dates"]),
        *calibration,
        logistic=None if linear is None else LogisticTomorrowModel(
            _number(linear["intercept"]), _numbers(linear["weights"]), _numbers(linear["means"]), _numbers(linear["scales"]),
        ),
        lightgbm=tree,
        tcn=None if source["tcn"] is None else _decode_tcn(source["tcn"]),
    )
    return LoadedTomorrowProbabilityModel(
        identity, artifact, None if tree is None else _load_tree(tree, profile),
        None if artifact.tcn is None else _load_network(artifact.tcn),
    )


def _load_tree(tree: str, profile: ScoringProfileId) -> lgb.Booster:
    try:
        booster = lgb.Booster(model_str=tree)
    except lgb.basic.LightGBMError as exc:
        raise ValueError("tomorrow_probability_artifact_tree_invalid") from exc
    if (
        booster.num_trees() < 1 or tuple(booster.feature_name()) != TOMORROW_FEATURE_NAMES
        or not str(booster.dump_model().get("objective", "")).startswith("lambdarank" if profile == "v1" else "binary ")
    ):
        raise ValueError("tomorrow_probability_artifact_tree_invalid")
    return booster


def _decode_tcn(value: object) -> TomorrowTcnModel:
    if not isinstance(value, dict) or set(value) != {"means", "scales", "network"}:
        raise ValueError("tomorrow_probability_tcn_payload_invalid")
    dropout, tensors, architecture = DecodeTcnNetwork(value["network"], len(TOMORROW_FEATURE_NAMES))
    return TomorrowTcnModel(_numbers(value["means"]), _numbers(value["scales"]), dropout, tensors, architecture)


def _load_network(model: TomorrowTcnModel) -> TcnNetwork:
    if find_spec("torch") is None:
        raise ValueError("tomorrow_v5_tensor_dependency_unavailable")
    try:
        from trader.training.infra.profile.v5.network import ConfigureTcnRuntime, TcnNetwork
    except ImportError as exc:
        raise ValueError("tomorrow_v5_tensor_dependency_unavailable") from exc

    ConfigureTcnRuntime(training=False)
    network = TcnNetwork(len(TOMORROW_FEATURE_NAMES), model.dropout, model.architecture)
    network.load_tensors(model.tensors)
    network.eval()
    return network


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


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
    "TomorrowFeatureNames",
    "TomorrowTcnModel",
]
