"""Publish and bind a D25 event model to its independent utility model."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, cast

import lightgbm as lgb
import numpy as np

from trader.infra.regular_file import ReadRegularFile
from trader.infra.atomic_files.json import SyncDirectory
from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.sealing import PublishImmutable
from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile, ModelInput, ModelPrediction
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.recommendation.domain.scoring.residualization import ExposureContract
from trader.training.infra.implementation_identity import TrainingImplementationIdentity
from trader.training.infra.model_bundles.predictor import TrainedHeadPredictor
from trader.training.infra.profile.v1.models import LearnedRankPredictor
from trader.training.infra.profile.v5.contracts import SEQUENCE_CONTRACT, SEQUENCE_FEATURE_IDS
from trader.training.infra.profile.v5.settings import TCN_NETWORK_CONTRACT, TcnArchitecture
from trader.training.infra.profile.v5.tensors import DecodeTcnNetwork, TcnTensor

if TYPE_CHECKING:
    from trader.training.infra.profile.v5.network import TcnNetwork

_MAX_BYTES = 128 * 1024**2
_ALGORITHMS = {"v1": "lambdarank", "v2": "lightgbm_binary", "v3": "l2_logistic", "v5": "tcn_bce"}
_KEYS = {"profile", "algorithm", "target", "features", "utility_hash", "input_hash", "utility_metadata_hash", "implementation_hash", "fitting_dates",
         "calibration_dates", "calibration", "models"}


@dataclass(frozen=True, slots=True)
class D25TreeModel:
    tree: str


@dataclass(frozen=True, slots=True)
class D25LogisticModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    intercept: float
    weights: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class D25TcnModel:
    means: tuple[float, ...]
    scales: tuple[float, ...]
    dropout: float
    tensors: tuple[TcnTensor, ...]
    architecture: TcnArchitecture


D25Model = D25TreeModel | D25LogisticModel | D25TcnModel


@dataclass(frozen=True, slots=True)
class D25ProbabilityBinding:
    target: PredictionTarget
    utility_hash: str
    input_hash: str
    utility_metadata_hash: str

    def __post_init__(self) -> None:
        if self.target.strategy is not Strategy.D25 or not all(
            _valid_hash(value) for value in (self.utility_hash, self.input_hash, self.utility_metadata_hash)
        ):
            raise ValueError("d25_probability_identity_invalid")


@dataclass(frozen=True, slots=True)
class D25ProbabilityArtifact:
    profile: ScoringProfileId
    binding: D25ProbabilityBinding
    features: tuple[str, ...]
    fitting_dates: tuple[date, ...]
    calibration_dates: tuple[date, ...]
    calibration: tuple[float, float]
    industry_models: tuple[tuple[str, D25Model], ...]


@dataclass(frozen=True, slots=True)
class _D25Predictor:
    utility: LearnedRankPredictor | TrainedHeadPredictor
    artifact: D25ProbabilityArtifact
    probability_hash: str
    models: Mapping[str, lgb.Booster | D25LogisticModel | TcnNetwork]
    tcn_transforms: Mapping[str, tuple[np.ndarray, np.ndarray]]

    @property
    def profile_id(self) -> ScoringProfileId:
        return self.artifact.profile

    @property
    def model_id(self) -> str:
        return f"{self.utility.model_id}_and_{_ALGORITHMS[self.profile_id]}"

    @property
    def model_hash(self) -> str:
        return ContentHash((self.utility.model_hash, self.probability_hash))

    @property
    def feature_ids(self) -> tuple[str, ...]:
        return self.utility.feature_ids

    @property
    def exposure_contract(self) -> ExposureContract:
        if not isinstance(self.utility, TrainedHeadPredictor):
            raise ValueError("V1 does not use industry residualization")
        return self.utility.exposure_contract

    @property
    def industry_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.models))

    def predict(self, inputs: Sequence[ModelInput]) -> tuple[ModelPrediction, ...]:
        predictions = self.utility.predict(inputs)
        intercept, slope = self.artifact.calibration
        grouped: dict[str, list[ModelInput]] = {}
        for item in inputs:
            industry = "" if self.profile_id == "v1" else item.industry
            if industry in self.models:
                grouped.setdefault(industry, []).append(item)
        event_scores: dict[str, tuple[float, float]] = {}
        for industry, selected in grouped.items():
            model = self.models[industry]
            for start in range(0, len(selected), 256):
                batch = selected[start : start + 256]
                if industry in self.tcn_transforms:
                    if any(item.sequence is None for item in batch):
                        raise ValueError("d25_probability_sequence_missing")
                    values = np.asarray([
                        (*item.alpha_features, *item.sequence.values) for item in batch if item.sequence is not None
                    ])
                    means, scales = self.tcn_transforms[industry]
                    network = cast("TcnNetwork", model)
                    scores = network.predict((values - means) / scales)
                else:
                    values = np.asarray([item.alpha_features for item in batch])
                    scores = (
                        model.intercept + (values - model.means) / model.scales @ np.asarray(model.weights)
                        if isinstance(model, D25LogisticModel)
                        else np.asarray(cast(lgb.Booster, model).predict(values, raw_score=True, num_threads=1))
                    )
                logits = intercept + slope * scores
                if not np.isfinite(logits).all():
                    raise ValueError("d25_probability_prediction_nonfinite")
                probabilities = np.exp(-np.logaddexp(0.0, -logits))
                for item, score, probability in zip(batch, scores, probabilities, strict=True):
                    event_scores[item.code] = float(score), float(probability)
        combined: list[ModelPrediction] = []
        for prediction in predictions:
            event = event_scores.get(prediction.code)
            if event is None:
                combined.append(prediction)
                continue
            score, probability = event
            combined.append(replace(
                prediction, rank_raw=score if self.profile_id == "v1" else prediction.rank_raw,
                target_probability=probability,
            ))
        return tuple(combined)


def _path(profile_root: Path, binding: D25ProbabilityBinding) -> Path:
    fingerprint = ContentHash((binding, TrainingImplementationIdentity()))
    return profile_root.parent / "d25-targets" / profile_root.name / f"{fingerprint}.json"


def PublishD25Probability(profile_root: Path, artifact: D25ProbabilityArtifact) -> str:
    binding = artifact.binding
    document = {
        "profile": artifact.profile,
        "algorithm": _ALGORITHMS[artifact.profile],
        "target": binding.target.identity,
        "features": artifact.features,
        "utility_hash": binding.utility_hash,
        "input_hash": binding.input_hash,
        "utility_metadata_hash": binding.utility_metadata_hash,
        "implementation_hash": TrainingImplementationIdentity(),
        "fitting_dates": tuple(day.isoformat() for day in artifact.fitting_dates),
        "calibration_dates": tuple(day.isoformat() for day in artifact.calibration_dates),
        "calibration": artifact.calibration,
        "models": tuple((industry, _model_document(model)) for industry, model in artifact.industry_models),
    }
    identity = ContentHash(document)
    rendered = json.dumps({"identity": identity, "artifact": document}, allow_nan=False, separators=(",", ":"))
    if len(rendered.encode()) > _MAX_BYTES:
        raise ValueError("d25_probability_artifact_too_large")
    path = _path(profile_root, binding)
    _decode(rendered, artifact.profile, artifact.features, binding)
    if not PublishImmutable(path, rendered):
        _, existing = _read(profile_root, artifact.profile, artifact.features, binding)
        if existing != identity:
            raise ValueError("d25_probability_publication_identity_conflict")
    SyncDirectory(path.parent)
    SyncDirectory(path.parent.parent)
    SyncDirectory(path.parent.parent.parent)
    return identity


def HasD25Probability(
    profile_root: Path, profile: ScoringProfileId, feature_ids: tuple[str, ...], binding: D25ProbabilityBinding,
) -> bool:
    try:
        _read(profile_root, profile, feature_ids, binding)
    except (OSError, ValueError, TypeError):
        return False
    return True


def LoadD25ScoringProfile(
    model_root: Path, loaded_profile: LoadedScoringProfile, target: PredictionTarget,
) -> LoadedScoringProfile:
    head = loaded_profile.heads[Strategy.D25]
    utility = head.predictor
    if not isinstance(utility, (LearnedRankPredictor, TrainedHeadPredictor)):
        raise ValueError("d25_probability_requires_current_utility_model")
    profile = loaded_profile.profile_id
    if utility.profile_id != profile:
        raise ValueError("d25_probability_utility_profile_mismatch")
    features = (
        (*utility.feature_ids, *(feature.value for feature in SEQUENCE_FEATURE_IDS))
        if profile == "v5" else utility.feature_ids
    )
    publication = next((item for item in loaded_profile.publications if item.strategy is Strategy.D25), None)
    if publication is None:
        raise ValueError("d25_probability_requires_current_utility_publication")
    artifact, identity = _read(
        model_root / profile, profile, features,
        D25ProbabilityBinding(target, utility.model_hash, publication.training_history_hash, publication.metadata_hash),
    )
    models: dict[str, lgb.Booster | D25LogisticModel | TcnNetwork] = {}
    transforms: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for industry, model in artifact.industry_models:
        if isinstance(utility, TrainedHeadPredictor) and industry not in utility.industry_ids:
            raise ValueError("d25_probability_utility_industry_mismatch")
        if isinstance(model, D25TreeModel):
            models[industry] = _booster(model.tree, profile, len(features))
        elif isinstance(model, D25LogisticModel):
            models[industry] = model
        else:
            from trader.training.infra.profile.v5.network import TcnNetwork

            network = TcnNetwork(len(utility.feature_ids), model.dropout, model.architecture)
            network.load_tensors(model.tensors)
            network.eval()
            models[industry] = network
            transforms[industry] = np.asarray(model.means), np.asarray(model.scales)
    predictor = _D25Predictor(utility, artifact, identity, models, transforms)
    return replace(loaded_profile, heads={**loaded_profile.heads, Strategy.D25: replace(head, predictor=predictor)})


def _model_document(model: D25Model) -> dict[str, object]:
    if not isinstance(model, D25TcnModel):
        return asdict(model)
    return {
        "means": model.means, "scales": model.scales,
        "network": {
            "contract": SEQUENCE_CONTRACT,
            "architecture": TCN_NETWORK_CONTRACT,
            "hidden_channels": model.architecture.hidden_channels,
            "compute_dtype": model.architecture.compute_dtype,
            "dropout": model.dropout,
            "weights": tuple(asdict(tensor) for tensor in model.tensors),
        },
    }


def _read(
    profile_root: Path, profile: ScoringProfileId, features: tuple[str, ...], binding: D25ProbabilityBinding,
) -> tuple[D25ProbabilityArtifact, str]:
    return _decode(
        ReadRegularFile(_path(profile_root, binding), maximum_bytes=_MAX_BYTES), profile, features, binding,
    )


def _decode(
    encoded: str | bytes, profile: ScoringProfileId, features: tuple[str, ...], binding: D25ProbabilityBinding,
) -> tuple[D25ProbabilityArtifact, str]:
    package = _object(json.loads(encoded), {"identity", "artifact"})
    identity = package["identity"]
    doc = _object(package["artifact"], _KEYS)
    if (
        not isinstance(identity, str) or ContentHash(doc) != identity
        or doc["profile"] != profile or doc["algorithm"] != _ALGORITHMS[profile]
        or doc["target"] != binding.target.identity or doc["features"] != list(features)
        or doc["utility_hash"] != binding.utility_hash or doc["input_hash"] != binding.input_hash
        or doc["utility_metadata_hash"] != binding.utility_metadata_hash
        or doc["implementation_hash"] != TrainingImplementationIdentity()
    ):
        raise ValueError("d25_probability_artifact_contract_mismatch")
    fitting, calibration = _dates(doc["fitting_dates"]), _dates(doc["calibration_dates"])
    intercept, slope = _vector(doc["calibration"], 2)
    if fitting[-1] >= calibration[0] or slope <= 0:
        raise ValueError("d25_probability_calibration_invalid")
    models = doc["models"]
    if not isinstance(models, list) or not models:
        raise ValueError("d25_probability_models_missing")
    parsed: list[tuple[str, D25Model]] = []
    for row in models:
        if not isinstance(row, list) or len(row) != 2 or not isinstance(row[0], str):
            raise ValueError("d25_probability_model_invalid")
        parsed.append((row[0], _decode_model(row[1], profile, len(features))))
    industries = tuple(key for key, _ in parsed)
    if industries != tuple(sorted(set(industries))) or (industries != ("",) if profile == "v1" else "" in industries):
        raise ValueError("d25_probability_industries_invalid")
    return D25ProbabilityArtifact(
        profile, binding, features, fitting, calibration, (intercept, slope), tuple(parsed),
    ), identity


def _decode_model(value: object, profile: ScoringProfileId, width: int) -> D25Model:
    if profile in {"v1", "v2"}:
        tree = _object(value, {"tree"})["tree"]
        if not isinstance(tree, str):
            raise ValueError("d25_probability_tree_invalid")
        _booster(tree, profile, width)
        return D25TreeModel(tree)
    raw = _object(value, {"means", "scales", "intercept", "weights"} if profile == "v3" else {"means", "scales", "network"})
    means, scales = _vector(raw["means"], width), _vector(raw["scales"], width)
    if min(scales) <= 0:
        raise ValueError("d25_probability_transform_invalid")
    if profile == "v3":
        return D25LogisticModel(means, scales, _number(raw["intercept"]), _vector(raw["weights"], width))
    dropout, tensors, architecture = DecodeTcnNetwork(raw["network"], width - len(SEQUENCE_FEATURE_IDS))
    return D25TcnModel(means, scales, dropout, tensors, architecture)


def _booster(tree: str, profile: ScoringProfileId, width: int) -> lgb.Booster:
    try:
        model = lgb.Booster(model_str=tree)
    except lgb.basic.LightGBMError as exc:
        raise ValueError("d25_probability_tree_invalid") from exc
    objective = "lambdarank" if profile == "v1" else "binary "
    if model.num_trees() < 1 or model.num_feature() != width or not str(model.dump_model().get("objective", "")).startswith(objective):
        raise ValueError("d25_probability_tree_invalid")
    return model


def _object(value: object, keys: set[str]) -> Mapping[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("d25_probability_artifact_fields_invalid")
    return value


def _number(value: object) -> float:
    if type(value) not in (int, float) or not math.isfinite(cast(float, value)):
        raise ValueError("d25_probability_number_invalid")
    return float(cast(float, value))


def _vector(value: object, count: int) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != count:
        raise ValueError("d25_probability_vector_invalid")
    return tuple(_number(item) for item in value)


def _dates(value: object) -> tuple[date, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(day, str) for day in value):
        raise ValueError("d25_probability_dates_invalid")
    days = tuple(date.fromisoformat(day) for day in value)
    if days != tuple(sorted(set(days))):
        raise ValueError("d25_probability_dates_invalid")
    return days


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
