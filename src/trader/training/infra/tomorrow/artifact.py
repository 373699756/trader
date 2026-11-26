"""Profile-owned Tomorrow models and atomic publication."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from itertools import islice
from pathlib import Path
from typing import TYPE_CHECKING

import lightgbm as lgb
import numpy as np

from trader.infra.regular_file import ReadRegularFile
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.market.tomorrow_probability import (
    TOMORROW_FEATURE_NAMES,
    TOMORROW_SEQUENCE_FEATURE_NAMES,
)
from trader.recommendation.domain.market.v5.features import SEQUENCE_CONTRACT
from trader.recommendation.domain.outcomes.prediction_target import ParsePredictionTargetIdentity, PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.model_publication import ModelPublicationIdentity
from trader.training.domain.tomorrow_probability import TomorrowDiagnosticAssessment
from trader.training.infra.model_bundles.profile_publication import ProfilePublicationLock, PublishStrategyModelFiles
from trader.training.infra.tcn.settings import TCN_NETWORK_CONTRACT
from trader.training.infra.tcn.tensors import DecodeTcnNetwork
from trader.training.infra.tomorrow.settings import LoadTomorrowFitSettings, TomorrowAlgorithm
from trader.training.infra.tomorrow.dates import PRODUCTION_CALIBRATION_DAYS, TomorrowWindows
from trader.training.infra.tomorrow.tree import TomorrowTreeScores
from trader.training.infra.tomorrow.v1.model import LoadTomorrowRanker
from trader.training.infra.tomorrow.v2.model import LoadTomorrowClassifier
from trader.training.infra.tomorrow.v3.model import LogisticTomorrowModel, TomorrowLogisticScores
from trader.training.infra.tomorrow.v5.model import LoadTomorrowNetwork, TomorrowTcnModel

if TYPE_CHECKING:
    from trader.training.infra.tcn.network import TcnNetwork

_MAX_MODEL_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 1024 * 1024
_METADATA_FIELDS = (
    "profile", "algorithm", "target", "features", "history_snapshot_hash", "training_contract_hash",
    "fitting_dates", "early_stopping_dates", "calibration_dates", "diagnostic", "fit_settings_hash",
)
_MODEL_FIELDS = ("logistic", "lightgbm", "tcn", "calibration")


def TomorrowFeatureNames(profile: ScoringProfileId) -> tuple[str, ...]:
    return TOMORROW_SEQUENCE_FEATURE_NAMES if profile == "v5" else TOMORROW_FEATURE_NAMES


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
    fit_settings_hash: str = field(kw_only=True)
    early_stopping_dates: tuple[date, ...] = field(kw_only=True)
    diagnostic: TomorrowDiagnosticAssessment = field(kw_only=True)

    def __post_init__(self) -> None:
        ordered = self.fitting_dates + self.early_stopping_dates + self.calibration_dates
        if (
            self.target.strategy is not Strategy.TOMORROW
            or not _valid_hash(self.history_snapshot_hash) or not _valid_hash(self.training_contract_hash)
            or not _valid_hash(self.fit_settings_hash)
            or not self.fitting_dates or not self.calibration_dates
            or ordered != tuple(sorted(set(ordered)))
            or len(self.fitting_dates) != len(self.diagnostic.fitting_dates)
            or len(self.calibration_dates) != PRODUCTION_CALIBRATION_DAYS
            or self.diagnostic.assessment_dates[-1] != self.calibration_dates[-1]
            or len(self.early_stopping_dates) != len(self.diagnostic.early_stopping_dates)
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

    @property
    def publication(self) -> ModelPublicationIdentity:
        artifact = self.artifact
        return _publication_identity(artifact.profile, self.identity, artifact.calibration_dates, artifact.history_snapshot_hash)

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
            scores = TomorrowLogisticScores(coefficients, matrix)
        elif self.network is not None:
            transforms = self.artifact.tcn
            assert transforms is not None
            means, scales = np.asarray(transforms.means), np.asarray(transforms.scales)
            scores = self.network.predict((matrix - means) / scales)
        else:
            assert self.booster is not None
            scores = TomorrowTreeScores(self.booster, matrix)
        logits = self.artifact.calibration_intercept + self.artifact.calibration_slope * scores
        if len(logits) != len(rows) or not np.all(np.isfinite(logits)):
            raise ValueError("tomorrow_prediction_invalid")
        return tuple(float(value) for value in np.exp(-np.logaddexp(0.0, -logits)))


def _document(artifact: TomorrowProbabilityArtifact) -> dict[str, object]:
    logistic = artifact.logistic
    tcn = artifact.tcn
    diagnostic = artifact.diagnostic
    return {
        "profile": artifact.profile,
        "algorithm": artifact.algorithm,
        "target": artifact.target.identity,
        "features": TomorrowFeatureNames(artifact.profile),
        "history_snapshot_hash": artifact.history_snapshot_hash,
        "training_contract_hash": artifact.training_contract_hash,
        "fit_settings_hash": artifact.fit_settings_hash,
        "fitting_dates": tuple(day.isoformat() for day in artifact.fitting_dates),
        "early_stopping_dates": tuple(day.isoformat() for day in artifact.early_stopping_dates),
        "calibration_dates": tuple(day.isoformat() for day in artifact.calibration_dates),
        "diagnostic": {
            "model_identity": diagnostic.model_identity,
            "fitting_dates": tuple(day.isoformat() for day in diagnostic.fitting_dates),
            "early_stopping_dates": tuple(day.isoformat() for day in diagnostic.early_stopping_dates),
            "calibration_dates": tuple(day.isoformat() for day in diagnostic.calibration_dates),
            "assessment_dates": tuple(day.isoformat() for day in diagnostic.assessment_dates),
            "rows": diagnostic.rows, "positive_rows": diagnostic.positive_rows, "brier": diagnostic.brier,
            "hits": diagnostic.hits, "seats": diagnostic.seats, "unsettled": diagnostic.unsettled,
        },
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


def TomorrowProbabilityModelPath(model_root: Path, profile: ScoringProfileId) -> Path:
    return model_root / profile / Strategy.TOMORROW.value / "model.json"


def PublishTomorrowProbabilityModel(model_root: Path, artifact: TomorrowProbabilityArtifact) -> str:
    document = _document(artifact)
    identity = ContentHash(document)
    model_text = json.dumps({key: document[key] for key in _MODEL_FIELDS}, ensure_ascii=True, allow_nan=False)
    metadata = {"identity": identity, **{key: document[key] for key in _METADATA_FIELDS}}
    metadata_text = json.dumps(metadata, ensure_ascii=True, allow_nan=False)
    if len(model_text) > _MAX_MODEL_BYTES or len(metadata_text) > _MAX_METADATA_BYTES:
        raise ValueError("tomorrow_probability_artifact_too_large")
    if artifact.lightgbm is not None:
        _load_tree(artifact.lightgbm, artifact.profile)
    if artifact.tcn is not None:
        _decode_tcn(json.loads(model_text)["tcn"])
    PublishStrategyModelFiles(model_root / artifact.profile, Strategy.TOMORROW, model_text, metadata_text)
    return identity


def LoadTomorrowProbabilityModel(
    model_root: Path, profile: ScoringProfileId, target: PredictionTarget, *, project_root: Path,
) -> LoadedTomorrowProbabilityModel | None:
    if target.strategy is not Strategy.TOMORROW:
        raise ValueError("tomorrow_probability_target_invalid")
    pair = _read_documents(model_root, profile, include_model=True)
    if pair is None:
        return None
    model_text, metadata_text = pair
    assert model_text is not None
    model: object = json.loads(model_text)
    metadata, identity, published_target = _parse_metadata(metadata_text, profile)
    if not isinstance(model, dict) or set(model) != set(_MODEL_FIELDS):
        raise ValueError("tomorrow_probability_artifact_invalid")
    source = {**model, **{key: metadata[key] for key in _METADATA_FIELDS}}
    if ContentHash(source) != identity:
        raise ValueError("tomorrow_probability_artifact_contract_mismatch")
    if target != published_target:
        return None
    settings = LoadTomorrowFitSettings(project_root, profile)
    fit_identity = settings.fitting_identity
    if fit_identity != source["fit_settings_hash"] or settings.identity != source["training_contract_hash"]:
        raise ValueError("tomorrow_probability_training_contract_mismatch")
    training_identity = source["training_contract_hash"]
    snapshot = source["history_snapshot_hash"]
    tree = source["lightgbm"]
    linear = source["logistic"]
    calibration = _numbers(source["calibration"])
    if (
        not isinstance(snapshot, str) or not isinstance(training_identity, str)
        or (tree is not None and not isinstance(tree, str)) or len(calibration) != 2
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
        fit_settings_hash=fit_identity,
        early_stopping_dates=_dates(source["early_stopping_dates"]), diagnostic=_diagnostic(source["diagnostic"]),
    )
    windows = TomorrowWindows(settings)
    if (
        len(artifact.fitting_dates) != windows.fitting or len(artifact.early_stopping_dates) != windows.early
        or len(artifact.diagnostic.calibration_dates) != windows.calibration
        or len(artifact.diagnostic.assessment_dates) != windows.assessment
    ):
        raise ValueError("tomorrow_probability_artifact_date_windows_invalid")
    return LoadedTomorrowProbabilityModel(
        identity, artifact, None if tree is None else _load_tree(tree, profile),
        None if artifact.tcn is None else LoadTomorrowNetwork(artifact.tcn),
    )


def ReadTomorrowPublicationIdentity(
    model_root: Path, profile: ScoringProfileId, target: PredictionTarget, *, cancel_requested: Callable[[], bool],
) -> ModelPublicationIdentity | None:
    pair = _read_documents(model_root, profile, include_model=False, cancel_requested=cancel_requested)
    if pair is None:
        return None
    metadata, identity, published_target = _parse_metadata(pair[1], profile)
    if target != published_target:
        return None
    snapshot = metadata["history_snapshot_hash"]
    dates = _dates(metadata["calibration_dates"])
    if (
        not isinstance(snapshot, str) or not _valid_hash(snapshot)
        or len(dates) != PRODUCTION_CALIBRATION_DAYS or dates != tuple(sorted(set(dates)))
    ):
        raise ValueError("tomorrow_probability_artifact_invalid")
    return _publication_identity(profile, identity, dates, snapshot)


def _publication_identity(
    profile: ScoringProfileId, identity: str, fitted: tuple[date, ...], history_snapshot_hash: str,
) -> ModelPublicationIdentity:
    return ModelPublicationIdentity(
        profile, Strategy.TOMORROW, identity, identity, fitted[-1], history_snapshot_hash, identity,
        probability_hash=identity,
    )


def _parse_metadata(
    encoded: bytes, profile: ScoringProfileId,
) -> tuple[dict[str, object], str, PredictionTarget]:
    metadata: object = json.loads(encoded)
    if not isinstance(metadata, dict) or set(metadata) != {"identity", *_METADATA_FIELDS}:
        raise ValueError("tomorrow_probability_artifact_invalid")
    identity: object = metadata["identity"]
    target_identity: object = metadata["target"]
    target = ParsePredictionTargetIdentity(target_identity) if isinstance(target_identity, str) else None
    if (
        not isinstance(identity, str) or not _valid_hash(identity)
        or metadata["profile"] != profile or metadata["algorithm"] != TomorrowAlgorithm(profile)
        or metadata["features"] != list(TomorrowFeatureNames(profile))
        or target is None or target.strategy is not Strategy.TOMORROW or target.identity != target_identity
    ):
        raise ValueError("tomorrow_probability_artifact_contract_mismatch")
    return dict(metadata), identity, target


def _read_documents(
    model_root: Path,
    profile: ScoringProfileId,
    *,
    include_model: bool,
    cancel_requested: Callable[[], bool] | None = None,
) -> tuple[bytes | None, bytes] | None:
    profile_root = model_root / profile
    if not profile_root.exists() and not profile_root.is_symlink():
        return None
    model_path = TomorrowProbabilityModelPath(model_root, profile)
    head_root = model_path.parent
    timeout = 2.0 if include_model else 1.0
    with ProfilePublicationLock(profile_root, timeout, cancel_requested=cancel_requested):
        if profile_root.is_symlink() or head_root.is_symlink():
            raise ValueError("tomorrow_probability_artifact_invalid")
        try:
            if {path.name for path in islice(head_root.iterdir(), 3)} - {"model.json", "metadata.json"}:
                raise ValueError("tomorrow_probability_artifact_invalid")
            if model_path.is_symlink() or not model_path.is_file():
                raise FileNotFoundError(model_path)
            model = ReadRegularFile(model_path, maximum_bytes=_MAX_MODEL_BYTES) if include_model else None
            metadata = ReadRegularFile(head_root / "metadata.json", maximum_bytes=_MAX_METADATA_BYTES)
        except FileNotFoundError:
            return None
    return model, metadata


def _load_tree(tree: str, profile: ScoringProfileId) -> lgb.Booster:
    return LoadTomorrowRanker(tree) if profile == "v1" else LoadTomorrowClassifier(tree)


def _decode_tcn(value: object) -> TomorrowTcnModel:
    if not isinstance(value, dict) or set(value) != {"means", "scales", "network"}:
        raise ValueError("tomorrow_probability_tcn_payload_invalid")
    dropout, tensors, architecture = DecodeTcnNetwork(value["network"], len(TOMORROW_FEATURE_NAMES))
    return TomorrowTcnModel(_numbers(value["means"]), _numbers(value["scales"]), dropout, tensors, architecture)


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


def _diagnostic(value: object) -> TomorrowDiagnosticAssessment:
    if not isinstance(value, dict) or set(value) != {
        "model_identity", "fitting_dates", "early_stopping_dates", "calibration_dates", "assessment_dates",
        "rows", "positive_rows", "brier", "hits", "seats", "unsettled",
    } or not isinstance(value["model_identity"], str):
        raise ValueError("tomorrow_diagnostic_assessment_invalid")
    return TomorrowDiagnosticAssessment(
        value["model_identity"], _dates(value["fitting_dates"]), _dates(value["early_stopping_dates"]),
        _dates(value["calibration_dates"]), _dates(value["assessment_dates"]), value["rows"], value["positive_rows"],
        _number(value["brier"]), value["hits"], value["seats"], value["unsettled"],
    )


__all__ = [
    "LoadedTomorrowProbabilityModel",
    "LoadTomorrowProbabilityModel",
    "ReadTomorrowPublicationIdentity",
    "PublishTomorrowProbabilityModel",
    "TomorrowProbabilityArtifact",
    "TomorrowProbabilityModelPath",
    "TomorrowFeatureNames",
]
