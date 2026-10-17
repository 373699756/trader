"""Validate one head inside an already selected immutable profile generation."""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from trader.infra.atomic_files.json import AtomicReadJson
from trader.infra.serialization.canonical import ContentHash
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
    if not head_root.is_dir() or head_root.is_symlink():
        raise ValueError("trained head directory is invalid")
    raw = AtomicReadJson(head_root / "active-bundle.json")
    if not isinstance(raw, dict):
        raise ValueError("trained head pointer is invalid")
    pointer = cast(dict[str, object], raw)
    expected = {"model_hash", "report_hash", "training_input_hash", "content_hash"}
    if (
        set(pointer) != expected
        or any(not _sha256(pointer.get(key)) for key in expected)
        or pointer["content_hash"]
        != ContentHash({key: value for key, value in pointer.items() if key != "content_hash"})
    ):
        raise ValueError("trained head pointer is invalid")
    model_path = head_root / "model.json"
    for name in ("model.json", "report.json", "training-input.json", "active-bundle.json"):
        path = head_root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("trained head fixed file is invalid")
    bundle = LoadHeadBundle(model_path, strategy, profile)
    if (
        bundle.content_hash != pointer["model_hash"]
        or bundle.training_input_document_hash != pointer["training_input_hash"]
        or bundle.report_hash != pointer["report_hash"]
    ):
        raise ValueError("trained head pointer does not match its fixed files")
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
        bundle.training_run_hash,
        bundle,
    )


def _sha256(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = ["ActiveHeadBundle", "InspectActiveHeadBundle", "LoadedActiveHeadBundle", "MakeBundleStagingDirectory"]
