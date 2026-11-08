"""Validate one head inside an already selected immutable profile generation."""

from __future__ import annotations

import tempfile
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from itertools import islice
from pathlib import Path

from trader.recommendation.domain.publication.models import Strategy
from trader.infra.regular_file import ReadRegularFile
from trader.training.domain.model_publication import ModelPublicationIdentity
from trader.training.infra.model_bundles.bundle_codec import (
    DecodeHeadPublicationIdentity, LoadHeadBundle, TrainedHeadModelBundle,
)
from trader.training.infra.model_bundles.contracts import TrainedProfileContract


@dataclass(frozen=True)
class PublishedHeadBundle:
    model_path: Path
    strategy: Strategy
    training_input_hash: str
    source_identity_hash: str
    label_cutoff: date
    training_contract_hash: str
    model_hash: str
    report_hash: str
    training_input_document_hash: str
    metadata_hash: str
    training_run_hash: str
    price_snapshot_hash: str = field(kw_only=True)


@dataclass(frozen=True)
class LoadedPublishedHeadBundle(PublishedHeadBundle):
    model: TrainedHeadModelBundle


def MakeBundleStagingDirectory(profile_root: Path) -> Path:
    profile_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".bundle-staging.", dir=profile_root))


def InspectPublishedHeadBundle(
    head_root: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> LoadedPublishedHeadBundle:
    if not head_root.exists() and not head_root.is_symlink():
        raise FileNotFoundError(head_root)
    if not head_root.is_dir() or head_root.is_symlink():
        raise ValueError("trained head directory is invalid")
    model_path = head_root / "model.json"
    files = {path.name for path in head_root.iterdir()}
    if files < {"model.json", "metadata.json"}:
        raise FileNotFoundError("trained head fixed files are missing")
    if files != {"model.json", "metadata.json"}:
        raise ValueError("trained head fixed files are incomplete")
    for name in ("model.json", "metadata.json"):
        path = head_root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("trained head fixed file is invalid")
    bundle = LoadHeadBundle(model_path, strategy, profile)
    metadata = bundle.metadata_hash
    return LoadedPublishedHeadBundle(
        model_path,
        strategy,
        bundle.training_input_hash,
        bundle.source_identity_hash,
        bundle.label_cutoff,
        bundle.training_contract_hash,
        bundle.content_hash,
        bundle.report_hash,
        bundle.training_input_document_hash,
        metadata,
        bundle.training_run_hash,
        bundle,
        price_snapshot_hash=bundle.price_snapshot_hash,
    )


def ReadPublishedHeadIdentity(
    head_root: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
    *,
    cancel_requested: Callable[[], bool],
) -> ModelPublicationIdentity:
    """Read only bounded publication metadata inside a caller-owned profile lock."""
    if cancel_requested():
        raise InterruptedError("model_publication_observation_cancelled")
    if not head_root.exists() and not head_root.is_symlink():
        raise FileNotFoundError(head_root)
    if not head_root.is_dir() or head_root.is_symlink():
        raise ValueError("trained head directory is invalid")
    files = {path.name for path in islice(head_root.iterdir(), 3)}
    if files < {"model.json", "metadata.json"}:
        raise FileNotFoundError("trained head fixed files are missing")
    if files != {"model.json", "metadata.json"}:
        raise ValueError("trained head fixed files are incomplete")
    model_path = head_root / "model.json"
    if model_path.is_symlink() or not model_path.is_file():
        raise ValueError("trained head fixed file is invalid")
    document = json.loads(ReadRegularFile(head_root / "metadata.json", maximum_bytes=8 * 1024 * 1024))
    if cancel_requested():
        raise InterruptedError("model_publication_observation_cancelled")
    return DecodeHeadPublicationIdentity(document, strategy, profile)


__all__ = [
    "PublishedHeadBundle",
    "InspectPublishedHeadBundle",
    "ReadPublishedHeadIdentity",
    "LoadedPublishedHeadBundle",
    "MakeBundleStagingDirectory",
]
