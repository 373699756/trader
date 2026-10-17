"""Publish both trained heads through one immutable generation and atomic pointer."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import cast

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import LoadHeadBundle
from trader.training.infra.model_bundles.bundle_snapshot import InspectActiveHeadBundle, LoadedActiveHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

_GENERATION = re.compile(r"^\.profile-bundle\.[A-Za-z0-9_-]+$")
_POINTER_NAME = "active-profile.json"


def PublishProfileBundles(
    profile_root: Path,
    profile: TrainedProfileContract,
    staged: tuple[tuple[Strategy, Path], ...],
) -> tuple[LoadedActiveHeadBundle, ...]:
    """Expose a complete immutable pair only after both heads validate."""

    if tuple(strategy for strategy, _ in staged) != tuple(head.strategy for head in profile.heads):
        raise ValueError("profile publication requires both ordered strategy heads")
    bundles = tuple(LoadHeadBundle(path / "model.json", strategy, profile) for strategy, path in staged)
    if (
        len({item.training_input_hash for item in bundles}) != 1
        or len({item.training_run_hash for item in bundles}) != 1
        or len({item.source_identity_hash for item in bundles}) != 1
    ):
        raise ValueError("profile publication heads have different source snapshots")
    profile_root.mkdir(parents=True, exist_ok=True)
    generation = Path(tempfile.mkdtemp(prefix=".profile-bundle.", dir=profile_root))
    try:
        for (strategy, staging), bundle in zip(staged, bundles, strict=True):
            destination = generation / strategy.value
            os.replace(staging, destination)
            pointer: dict[str, object] = {
                "model_hash": bundle.content_hash,
                "report_hash": bundle.report_hash,
                "training_input_hash": bundle.training_input_document_hash,
            }
            pointer["content_hash"] = ContentHash(pointer)
            AtomicWriteJson(destination / "active-bundle.json", pointer)
            _fsync_directory(destination)
        _fsync_directory(generation)
        heads = tuple(
            InspectActiveHeadBundle(generation / contract.directory_name, contract.strategy, profile)
            for contract in profile.heads
        )
        pointer = {
            "profile_id": profile.profile_id,
            "generation": generation.name,
            "training_input_hash": bundles[0].training_input_hash,
            "training_run_hash": bundles[0].training_run_hash,
            "source_identity_hash": bundles[0].source_identity_hash,
            "model_hashes": [item.model_hash for item in heads],
        }
        pointer["content_hash"] = ContentHash(pointer)
        AtomicWriteJson(profile_root / _POINTER_NAME, pointer)
        _fsync_directory(profile_root)
        return heads
    except BaseException:
        # The current pointer always names a complete older generation until
        # the final atomic replace; an orphan is harmless after a hard crash.
        if generation.exists() and not _points_to(profile_root, generation.name):
            shutil.rmtree(generation)
        raise


def InspectActiveProfileBundles(
    profile_root: Path, profile: TrainedProfileContract
) -> tuple[LoadedActiveHeadBundle, ...]:
    """Pin one generation for all readers, then verify its two head hashes."""

    raw = AtomicReadJson(profile_root / _POINTER_NAME)
    if not isinstance(raw, dict):
        raise ValueError("trained profile pointer is invalid")
    pointer = cast(dict[str, object], raw)
    generation_name = pointer.get("generation")
    model_hashes = pointer.get("model_hashes")
    expected = {
        "profile_id",
        "generation",
        "training_input_hash",
        "training_run_hash",
        "source_identity_hash",
        "model_hashes",
        "content_hash",
    }
    if (
        set(pointer) != expected
        or pointer.get("profile_id") != profile.profile_id
        or not isinstance(generation_name, str)
        or _GENERATION.fullmatch(generation_name) is None
        or not isinstance(model_hashes, list)
        or len(model_hashes) != 2
        or any(not isinstance(value, str) or len(value) != 64 for value in model_hashes)
        or pointer.get("content_hash")
        != ContentHash({key: value for key, value in pointer.items() if key != "content_hash"})
    ):
        raise ValueError("trained profile pointer is invalid")
    generation = profile_root / generation_name
    if not generation.is_dir() or generation.is_symlink():
        raise ValueError("trained profile generation is invalid")
    heads = tuple(
        InspectActiveHeadBundle(generation / contract.directory_name, contract.strategy, profile)
        for contract in profile.heads
    )
    if (
        tuple(item.model_hash for item in heads) != tuple(model_hashes)
        or any(item.training_input_hash != pointer["training_input_hash"] for item in heads)
        or any(item.training_run_hash != pointer["training_run_hash"] for item in heads)
        or any(item.source_identity_hash != pointer["source_identity_hash"] for item in heads)
    ):
        raise ValueError("trained profile pair identity is inconsistent")
    return heads


def _points_to(profile_root: Path, generation_name: str) -> bool:
    try:
        pointer = AtomicReadJson(profile_root / _POINTER_NAME)
    except (OSError, ValueError):
        return False
    return isinstance(pointer, dict) and pointer.get("generation") == generation_name


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InspectActiveProfileBundles", "PublishProfileBundles"]
