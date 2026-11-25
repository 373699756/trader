"""Publish and bind a D25 event model to its independent utility model."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import date
from importlib.util import find_spec
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
from trader.training.infra.d25.contracts import StandardD25Profile
from trader.training.infra.d25.model_document import (
    D25MetadataIntegrityError, D25ModelPair, D25ProbabilityPublication, EncodeD25MetadataDocument,
    EncodeD25ModelDocument, ReadD25ModelPair,
)
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
from trader.training.infra.model_bundles.bundle_codec import DecodeHeadBundle, ModelBundleIntegrityError
from trader.training.infra.model_bundles.predictor import TrainedHeadPredictor
from trader.training.infra.model_bundles.profile import BuildTrainedScoringProfile
from trader.training.infra.model_bundles.profile_publication import ProfilePublicationLock
from trader.training.infra.d25.v1.models import DecodeV1D25Utility, LearnedRankPredictor
from trader.training.infra.tcn.manifest import SEQUENCE_FEATURE_IDS

_MAX_BYTES = 128 * 1024**2
_ALGORITHMS = {"v1": V1_ALGORITHM, "v2": V2_ALGORITHM, "v3": V3_ALGORITHM, "v5": V5_ALGORITHM}
_KEYS = {"profile", "algorithm", "target", "features", "utility_hash", "input_hash", "utility_metadata_hash", "implementation_hash", "fitting_dates",
         "calibration_dates", "calibration", "models"}


D25Model = D25TreeModel | D25LogisticModel | D25TcnModel


class D25ProbabilityModelMissingError(FileNotFoundError):
    """A current utility model exists without a trained event model."""


class D25ProbabilityModelInvalidError(ValueError):
    """The current D25 event model does not match its declared publication."""


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
    staged = ReadD25ModelPair(model_directory)
    _require_bound_utility(staged.model.utility, artifact.profile, binding)
    if staged.model.probability is not None:
        _, existing = _read_probability(staged, artifact.profile, artifact.features, binding)
        if existing != identity:
            raise ValueError("d25_probability_publication_identity_conflict")
    else:
        model = EncodeD25ModelDocument(replace(staged.model, probability=probability))
        metadata = EncodeD25MetadataDocument(staged.metadata.utility, D25ProbabilityPublication(binding.target, identity))
        ReplaceFile(model_directory / "model.json", json.dumps(model, allow_nan=False, separators=(",", ":")))
        ReplaceFile(model_directory / "metadata.json", json.dumps(metadata, allow_nan=False, separators=(",", ":")))
    return identity


def HasD25Probability(
    profile_root: Path, profile: ScoringProfileId, feature_ids: tuple[str, ...], binding: D25ProbabilityBinding,
) -> bool:
    return ReadD25ProbabilityIdentity(profile_root, profile, feature_ids, binding) is not None


def ReadD25ProbabilityIdentity(
    profile_root: Path, profile: ScoringProfileId, feature_ids: tuple[str, ...], binding: D25ProbabilityBinding,
) -> str | None:
    try:
        _, identity = _read(profile_root, profile, feature_ids, binding)
    except (OSError, ValueError, TypeError):
        return None
    return identity


def LoadD25ScoringProfile(
    model_root: Path, profile: ScoringProfileId, target: PredictionTarget,
) -> LoadedScoringProfile:
    profile_root = model_root / profile
    documents = _read_pair(profile_root)
    try:
        if profile == "v1":
            loaded_profile = DecodeV1D25Utility(documents)
        else:
            if profile == "v5":
                if find_spec("torch") is None:
                    raise ImportError("D25 V5 requires torch")
                from trader.training.infra.tcn.network import ConfigureTcnRuntime

                ConfigureTcnRuntime(training=False)
            bundle = DecodeHeadBundle(documents.model.utility, documents.metadata.utility, Strategy.D25, StandardD25Profile(profile))
            loaded_profile = BuildTrainedScoringProfile(profile, (bundle,))
    except D25MetadataIntegrityError as exc:
        raise ModelBundleIntegrityError(str(exc)) from exc
    head = loaded_profile.heads[Strategy.D25]
    utility = head.predictor
    if not isinstance(utility, (LearnedRankPredictor, TrainedHeadPredictor)):
        raise ValueError("d25_probability_requires_current_utility_model")
    publication, = loaded_profile.publications
    features = (*utility.feature_ids, *(feature.value for feature in SEQUENCE_FEATURE_IDS)) if profile == "v5" else utility.feature_ids
    binding = D25ProbabilityBinding(target, utility.model_hash, publication.training_history_hash, publication.metadata_hash)
    try:
        artifact, identity = _read_probability(documents, profile, features, binding)
    except ValueError as exc:
        raise D25ProbabilityModelInvalidError(str(exc)) from exc
    if isinstance(utility, TrainedHeadPredictor) and any(
        industry not in utility.industry_ids for industry, _model in artifact.industry_models
    ):
        raise D25ProbabilityModelInvalidError("d25_probability_utility_industry_mismatch")
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
    return replace(
        loaded_profile,
        heads={Strategy.D25: replace(head, predictor=predictor)},
        publications=(documents.metadata.scoring_identity(publication, target),),
    )


def ReadD25ProbabilityArtifact(loaded_profile: LoadedScoringProfile) -> D25ProbabilityArtifact:
    """Get the event evidence already verified when opening the scoring generation."""
    predictor = loaded_profile.heads[Strategy.D25].predictor
    if not isinstance(predictor, _D25Predictor):
        raise ValueError("d25_scoring_profile_has_no_probability_model")
    return predictor.artifact


def _model_document(model: D25Model) -> dict[str, object]:
    return EncodeD25Tcn(model) if isinstance(model, D25TcnModel) else asdict(model)


def _read(
    profile_root: Path, profile: ScoringProfileId, features: tuple[str, ...], binding: D25ProbabilityBinding,
) -> tuple[D25ProbabilityArtifact, str]:
    if ParseScoringProfile(profile_root.name) != profile:
        raise ValueError("d25_probability_profile_path_mismatch")
    return _read_probability(_read_pair(profile_root), profile, features, binding)


def _read_pair(profile_root: Path) -> D25ModelPair:
    if not profile_root.parent.exists():
        raise FileNotFoundError(profile_root)
    with ProfilePublicationLock(profile_root):
        if profile_root.is_symlink():
            raise ValueError("d25_probability_directory_invalid")
        try:
            return ReadD25ModelPair(profile_root / Strategy.D25.value)
        except D25MetadataIntegrityError as exc:
            raise ModelBundleIntegrityError(str(exc)) from exc


def _read_probability(
    documents: D25ModelPair, profile: ScoringProfileId, features: tuple[str, ...], binding: D25ProbabilityBinding,
) -> tuple[D25ProbabilityArtifact, str]:
    _require_bound_utility(documents.model.utility, profile, binding)
    metadata, probability = documents.metadata.probability_publication, documents.model.probability
    if metadata is None and probability is None:
        raise D25ProbabilityModelMissingError("d25_probability_artifact_missing")
    if metadata is None or probability is None or metadata.target != binding.target:
        raise ValueError("d25_probability_artifact_contract_mismatch")
    artifact, identity = _decode(probability, profile, features, binding)
    if identity != metadata.model_hash:
        raise ValueError("d25_probability_publication_identity_conflict")
    return artifact, identity


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
        or not isinstance(doc["implementation_hash"], str) or not _valid_hash(doc["implementation_hash"])
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
