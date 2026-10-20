"""Validate one head inside an already selected immutable profile generation."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import LoadHeadBundle, TrainedHeadModelBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract


@dataclass(frozen=True)
class ActiveHeadBundle:
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


@dataclass(frozen=True)
class LoadedActiveHeadBundle(ActiveHeadBundle):
    model: TrainedHeadModelBundle


def MakeBundleStagingDirectory(profile_root: Path) -> Path:
    profile_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".bundle-staging.", dir=profile_root))


def InspectActiveHeadBundle(
    head_root: Path,
    strategy: Strategy,
    profile: TrainedProfileContract,
) -> LoadedActiveHeadBundle:
    if not head_root.exists() and not head_root.is_symlink():
        raise FileNotFoundError(head_root)
    if not head_root.is_dir() or head_root.is_symlink():
        raise ValueError("trained head directory is invalid")
    model_path = head_root / "model.json"
    if {path.name for path in head_root.iterdir()} != {"model.json", "metadata.json"}:
        raise ValueError("trained head fixed files are incomplete")
    for name in ("model.json", "metadata.json"):
        path = head_root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("trained head fixed file is invalid")
    bundle = LoadHeadBundle(model_path, strategy, profile)
    metadata = bundle.metadata_hash
    return LoadedActiveHeadBundle(
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
    )


__all__ = ["ActiveHeadBundle", "InspectActiveHeadBundle", "LoadedActiveHeadBundle", "MakeBundleStagingDirectory"]
