"""The single numerical document for a D25 strategy model."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from itertools import islice
from pathlib import Path

from trader.infra.regular_file import ReadRegularFile
from trader.infra.serialization.canonical import ContentHash
from trader.infra.serialization.fields import IsSha256Text
from trader.recommendation.domain.outcomes.prediction_target import ParsePredictionTargetIdentity, PredictionTarget
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.model_publication import ModelPublicationIdentity

_MAXIMUM_MODEL_BYTES = 160 * 1024**2
D25_MAXIMUM_METADATA_BYTES = 32 * 1024**2


class D25MetadataIntegrityError(ValueError):
    """The published D25 metadata does not match its content identity."""


@dataclass(frozen=True, slots=True)
class D25ModelDocument:
    utility: Mapping[str, object]
    probability: Mapping[str, object] | None = None


@dataclass(frozen=True, slots=True)
class D25ProbabilityPublication:
    target: PredictionTarget
    model_hash: str


@dataclass(frozen=True, slots=True)
class D25MetadataDocument:
    utility: Mapping[str, object]
    probability_publication: D25ProbabilityPublication | None
    content_hash: str

    def scoring_identity(
        self, utility_identity: ModelPublicationIdentity, target: PredictionTarget | None = None,
    ) -> ModelPublicationIdentity:
        event = self.probability_publication
        if event is None or target is not None and event.target != target:
            return utility_identity
        return replace(
            utility_identity,
            model_hash=ContentHash((utility_identity.model_hash, event.model_hash)),
            metadata_hash=self.content_hash,
            probability_hash=event.model_hash,
        )


@dataclass(frozen=True, slots=True)
class D25ModelPair:
    model: D25ModelDocument
    metadata: D25MetadataDocument


def EncodeD25ModelDocument(document: D25ModelDocument) -> dict[str, object]:
    return {
        "utility": dict(document.utility),
        "probability": dict(document.probability) if document.probability is not None else None,
    }


def ReadD25ModelDocument(path: Path) -> D25ModelDocument:
    value: object = json.loads(ReadRegularFile(path, maximum_bytes=_MAXIMUM_MODEL_BYTES))
    if not isinstance(value, dict) or set(value) != {"utility", "probability"}:
        raise ValueError("d25_model_document_fields_invalid")
    utility: object = value["utility"]
    probability: object = value["probability"]
    if not isinstance(utility, dict) or (probability is not None and not isinstance(probability, dict)):
        raise ValueError("d25_model_document_fields_invalid")
    return D25ModelDocument(utility, probability)


def EncodeD25MetadataDocument(
    utility: Mapping[str, object], probability: D25ProbabilityPublication | None = None,
) -> dict[str, object]:
    document = {
        **utility,
        "probability_publication": {"target": probability.target.identity, "model_hash": probability.model_hash}
        if probability is not None else None,
    }
    document.pop("content_hash", None)
    document["content_hash"] = ContentHash(document)
    return document


def ReadD25MetadataDocument(path: Path) -> D25MetadataDocument:
    value: object = json.loads(ReadRegularFile(path, maximum_bytes=D25_MAXIMUM_METADATA_BYTES))
    return DecodeD25MetadataDocument(value)


def DecodeD25MetadataDocument(value: object) -> D25MetadataDocument:
    if not isinstance(value, dict) or "probability_publication" not in value:
        raise ValueError("trained-head metadata fields are invalid")
    body = dict(value)
    identity: object = body.pop("content_hash", None)
    if not isinstance(identity, str) or ContentHash(body) != identity:
        raise D25MetadataIntegrityError("trained-head metadata content hash is invalid")
    probability = _decode_probability_publication(body.pop("probability_publication"))
    body["content_hash"] = ContentHash(body)
    return D25MetadataDocument(body, probability, identity)


def _decode_probability_publication(value: object) -> D25ProbabilityPublication | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"target", "model_hash"}:
        raise ValueError("d25_probability_publication_fields_invalid")
    target = ParsePredictionTargetIdentity(value["target"]) if isinstance(value["target"], str) else None
    model_hash = value["model_hash"]
    if target is None or target.strategy is not Strategy.D25 or not IsSha256Text(model_hash):
        raise ValueError("d25_probability_publication_identity_invalid")
    return D25ProbabilityPublication(target, model_hash)


def ReadD25ModelPair(directory: Path) -> D25ModelPair:
    if directory.is_symlink():
        raise ValueError("d25_model_directory_invalid")
    names = {path.name for path in islice(directory.iterdir(), 3)}
    expected = {"model.json", "metadata.json"}
    if names < expected:
        raise FileNotFoundError("d25_model_fixed_files_missing")
    if names != expected:
        raise ValueError("d25_model_fixed_files_invalid")
    return D25ModelPair(
        ReadD25ModelDocument(directory / "model.json"),
        ReadD25MetadataDocument(directory / "metadata.json"),
    )
