"""Publish and bind a D25 event model to its independent utility model."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from pathlib import Path
import numpy as np

from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.sealing import ReplaceFile
from trader.recommendation.application.ports.loaded_profile import LoadedScoringProfile, ModelInput, ModelPrediction
from trader.recommendation.domain.outcomes.prediction_target import PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.scoring.profile_identity import ParseScoringProfile, ScoringProfileId
from trader.recommendation.domain.scoring.residualization import ExposureContract
from trader.training.infra.d25.artifact_values import D25ArtifactObject, D25ArtifactVector
from trader.training.infra.d25.model_document import EncodeD25ModelDocument, ReadD25ModelDocument
from trader.training.infra.d25.tree import D25TreeModel
from trader.training.infra.d25.v1.model import ALGORITHM as V1_ALGORITHM
from trader.training.infra.d25.v1.model import DecodeD25Ranker, LoadD25RankerScorer
from trader.training.infra.d25.v2.probability import ALGORITHM as V2_ALGORITHM
from trader.training.infra.d25.v2.probability import DecodeD25Classifier, LoadD25ClassifierScorer
from trader.training.infra.d25.v3.probability import ALGORITHM as V3_ALGORITHM
from trader.training.infra.d25.v3.probability import D25LogisticModel, DecodeD25Logistic, LoadD25LogisticScorer
from trader.training.infra.d25.v5.probability import ALGORITHM as V5_ALGORITHM
from trader.training.infra.d25.v5.probability import D25TcnModel, DecodeD25Tcn, EncodeD25Tcn, LoadD25TcnScorer
from trader.training.infra.implementation_identity import LaneTrainingImplementationIdentity
from trader.training.infra.model_bundles.predictor import TrainedHeadPredictor
from trader.training.infra.model_bundles.profile_publication import ProfilePublicationLock
from trader.training.infra.d25.v1.models import LearnedRankPredictor
from trader.training.infra.tcn.manifest import SEQUENCE_FEATURE_IDS

_MAX_BYTES = 128 * 1024**2
_ALGORITHMS = {"v1": V1_ALGORITHM, "v2": V2_ALGORITHM, "v3": V3_ALGORITHM, "v5": V5_ALGORITHM}
_KEYS = {"profile", "algorithm", "target", "features", "utility_hash", "input_hash", "utility_metadata_hash", "implementation_hash", "fitting_dates",
         "calibration_dates", "calibration", "models"}


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
    models: Mapping[str, Callable[[Sequence[ModelInput]], np.ndarray]]

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
                scores = model(batch)
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


def StageD25Probability(model_directory: Path, artifact: D25ProbabilityArtifact) -> str:
    """Attach the calibrated model before publishing the enclosing D25 directory."""
    binding = artifact.binding
    document = {
        "profile": artifact.profile,
        "algorithm": _ALGORITHMS[artifact.profile],
        "target": binding.target.identity,
        "features": artifact.features,
        "utility_hash": binding.utility_hash,
        "input_hash": binding.input_hash,
        "utility_metadata_hash": binding.utility_metadata_hash,
        "implementation_hash": LaneTrainingImplementationIdentity(Strategy.D25, artifact.profile),
        "fitting_dates": tuple(day.isoformat() for day in artifact.fitting_dates),
        "calibration_dates": tuple(day.isoformat() for day in artifact.calibration_dates),
        "calibration": artifact.calibration,
        "models": tuple((industry, _model_document(model)) for industry, model in artifact.industry_models),
    }
    identity = ContentHash(document)
    rendered = json.dumps({"identity": identity, "artifact": document}, allow_nan=False, separators=(",", ":"))
    if len(rendered.encode()) > _MAX_BYTES:
        raise ValueError("d25_probability_artifact_too_large")
    probability = D25ArtifactObject(json.loads(rendered), {"identity", "artifact"})
    _decode(probability, artifact.profile, artifact.features, binding)
    path = model_directory / "model.json"
    staged = ReadD25ModelDocument(path)
    _require_bound_utility(staged.utility, artifact.profile, binding)
    if staged.probability is not None:
        _, existing = _decode(staged.probability, artifact.profile, artifact.features, binding)
        if existing != identity:
            raise ValueError("d25_probability_publication_identity_conflict")
    else:
        model = EncodeD25ModelDocument(replace(staged, probability=probability))
        ReplaceFile(path, json.dumps(model, allow_nan=False, separators=(",", ":")))
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
    artifact, identity = _read_utility_bound_artifact(model_root, loaded_profile, target, utility)
    models: dict[str, Callable[[Sequence[ModelInput]], np.ndarray]] = {}
    for industry, model in artifact.industry_models:
        if isinstance(model, D25TreeModel):
            load = LoadD25RankerScorer if artifact.profile == "v1" else LoadD25ClassifierScorer
            models[industry] = load(model, len(artifact.features))
        elif isinstance(model, D25LogisticModel):
            models[industry] = LoadD25LogisticScorer(model)
        else:
            models[industry] = LoadD25TcnScorer(model, len(utility.feature_ids))
    predictor = _D25Predictor(utility, artifact, identity, models)
    return replace(loaded_profile, heads={**loaded_profile.heads, Strategy.D25: replace(head, predictor=predictor)})


def ReadD25ProbabilityArtifact(
    model_root: Path, loaded_profile: LoadedScoringProfile, target: PredictionTarget,
) -> D25ProbabilityArtifact:
    """Read numerical model evidence after verifying the active D25 utility binding."""
    utility = loaded_profile.heads[Strategy.D25].predictor
    if not isinstance(utility, (LearnedRankPredictor, TrainedHeadPredictor)):
        raise ValueError("d25_probability_requires_current_utility_model")
    artifact, _identity = _read_utility_bound_artifact(model_root, loaded_profile, target, utility)
    return artifact


def _read_utility_bound_artifact(
    model_root: Path, loaded_profile: LoadedScoringProfile, target: PredictionTarget,
    utility: LearnedRankPredictor | TrainedHeadPredictor,
) -> tuple[D25ProbabilityArtifact, str]:
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
    if isinstance(utility, TrainedHeadPredictor) and any(
        industry not in utility.industry_ids for industry, _model in artifact.industry_models
    ):
        raise ValueError("d25_probability_utility_industry_mismatch")
    return artifact, identity


def _model_document(model: D25Model) -> dict[str, object]:
    return EncodeD25Tcn(model) if isinstance(model, D25TcnModel) else asdict(model)


def _read(
    profile_root: Path, profile: ScoringProfileId, features: tuple[str, ...], binding: D25ProbabilityBinding,
) -> tuple[D25ProbabilityArtifact, str]:
    if ParseScoringProfile(profile_root.name) != profile:
        raise ValueError("d25_probability_profile_path_mismatch")
    if not profile_root.exists():
        raise FileNotFoundError(profile_root)
    with ProfilePublicationLock(profile_root):
        directory = profile_root / Strategy.D25.value
        if profile_root.is_symlink() or not profile_root.is_dir() or directory.is_symlink():
            raise ValueError("d25_probability_directory_invalid")
        model = ReadD25ModelDocument(directory / "model.json")
        _require_bound_utility(model.utility, profile, binding)
        if model.probability is None:
            raise FileNotFoundError("d25_probability_artifact_missing")
        return _decode(model.probability, profile, features, binding)


def _require_bound_utility(
    utility: Mapping[str, object], profile: ScoringProfileId, binding: D25ProbabilityBinding,
) -> None:
    key = "head_content_hash" if profile == "v1" else "content_hash"
    if utility.get(key) != binding.utility_hash:
        raise ValueError("d25_probability_utility_hash_mismatch")


def _decode(
    encoded: object, profile: ScoringProfileId, features: tuple[str, ...], binding: D25ProbabilityBinding,
) -> tuple[D25ProbabilityArtifact, str]:
    package = D25ArtifactObject(encoded, {"identity", "artifact"})
    identity = package["identity"]
    doc = D25ArtifactObject(package["artifact"], _KEYS)
    if (
        not isinstance(identity, str) or ContentHash(doc) != identity
        or doc["profile"] != profile or doc["algorithm"] != _ALGORITHMS[profile]
        or doc["target"] != binding.target.identity or doc["features"] != list(features)
        or doc["utility_hash"] != binding.utility_hash or doc["input_hash"] != binding.input_hash
        or doc["utility_metadata_hash"] != binding.utility_metadata_hash
        or doc["implementation_hash"] != LaneTrainingImplementationIdentity(Strategy.D25, profile)
    ):
        raise ValueError("d25_probability_artifact_contract_mismatch")
    fitting, calibration = _dates(doc["fitting_dates"]), _dates(doc["calibration_dates"])
    intercept, slope = D25ArtifactVector(doc["calibration"], 2)
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
    decoders: dict[ScoringProfileId, Callable[[object, int], D25Model]] = {
        "v1": DecodeD25Ranker, "v2": DecodeD25Classifier, "v3": DecodeD25Logistic, "v5": DecodeD25Tcn,
    }
    return decoders[profile](value, width)


def _dates(value: object) -> tuple[date, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(day, str) for day in value):
        raise ValueError("d25_probability_dates_invalid")
    days = tuple(date.fromisoformat(day) for day in value)
    if days != tuple(sorted(set(days))):
        raise ValueError("d25_probability_dates_invalid")
    return days


def _valid_hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)
