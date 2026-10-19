"""Publish both trained heads in the fixed, portable profile layout."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import cast

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson
from trader.infra.serialization.canonical import ContentHash
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import LoadHeadBundle
from trader.training.infra.model_bundles.bundle_snapshot import InspectActiveHeadBundle, LoadedActiveHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

_POINTER_NAME = "active-profile.json"
_LOGGER = logging.getLogger(__name__)


def PublishProfileBundles(
    profile_root: Path,
    profile: TrainedProfileContract,
    staged: tuple[tuple[Strategy, Path], ...],
) -> tuple[LoadedActiveHeadBundle, ...]:
    """Expose a complete fixed-directory pair only after both heads validate."""

    if tuple(strategy for strategy, _ in staged) != tuple(head.strategy for head in profile.heads):
        raise ValueError("profile publication requires both ordered strategy heads")
    bundles = tuple(LoadHeadBundle(path / "model.json", strategy, profile) for strategy, path in staged)
    if (
        len({item.training_input_hash for item in bundles}) != 1
        or len({item.training_run_hash for item in bundles}) != 1
        or len({item.source_identity_hash for item in bundles}) != 1
    ):
        raise ValueError("profile publication heads have different source snapshots")
    profile_root.parent.mkdir(parents=True, exist_ok=True)
    if profile_root.is_symlink() or (profile_root.exists() and not profile_root.is_dir()):
        raise ValueError("trained profile directory is invalid")
    publication = Path(tempfile.mkdtemp(prefix=f".{profile_root.name}.publication.", dir=profile_root.parent))
    previous = profile_root.with_name(f".{profile_root.name}.previous.{uuid.uuid4().hex}")
    old_moved = False
    try:
        for (strategy, staging), bundle in zip(staged, bundles, strict=True):
            destination = publication / strategy.value
            shutil.copytree(staging, destination)
            if profile.profile_id == "v2":
                (destination / ".gitkeep").write_text("\n", encoding="utf-8")
            pointer: dict[str, object] = {
                "model_hash": bundle.content_hash,
                "report_hash": bundle.report_hash,
                "training_input_hash": bundle.training_input_document_hash,
            }
            pointer["content_hash"] = ContentHash(pointer)
            AtomicWriteJson(destination / "active-bundle.json", pointer)
            for path in destination.iterdir():
                if path.is_file():
                    with path.open("rb") as handle:
                        os.fsync(handle.fileno())
            _fsync_directory(destination)
        _fsync_directory(publication)
        heads = tuple(
            InspectActiveHeadBundle(publication / contract.directory_name, contract.strategy, profile)
            for contract in profile.heads
        )
        pointer = {
            "profile_id": profile.profile_id,
            "training_input_hash": bundles[0].training_input_hash,
            "training_run_hash": bundles[0].training_run_hash,
            "source_identity_hash": bundles[0].source_identity_hash,
            "model_hashes": [item.model_hash for item in heads],
        }
        pointer["content_hash"] = ContentHash(pointer)
        AtomicWriteJson(publication / _POINTER_NAME, pointer)
        _fsync_directory(publication)
        InspectActiveProfileBundles(publication, profile)
        if profile_root.exists():
            os.replace(profile_root, previous)
            old_moved = True
        new_moved = False
        try:
            os.replace(publication, profile_root)
            new_moved = True
            _fsync_directory(profile_root.parent)
        except OSError:
            if new_moved:
                os.replace(profile_root, publication)
            if old_moved:
                os.replace(previous, profile_root)
                old_moved = False
            raise
        if old_moved:
            try:
                shutil.rmtree(previous)
            except OSError:
                _LOGGER.warning("trained_profile_previous_cleanup_failed")
        return tuple(
            InspectActiveHeadBundle(profile_root / contract.directory_name, contract.strategy, profile)
            for contract in profile.heads
        )
    finally:
        if publication.exists():
            shutil.rmtree(publication)


def InspectActiveProfileBundles(
    profile_root: Path, profile: TrainedProfileContract
) -> tuple[LoadedActiveHeadBundle, ...]:
    """Pin one generation for all readers, then verify its two head hashes."""

    raw = AtomicReadJson(profile_root / _POINTER_NAME)
    if not isinstance(raw, dict):
        raise ValueError("trained profile pointer is invalid")
    pointer = cast(dict[str, object], raw)
    model_hashes = pointer.get("model_hashes")
    expected = {
        "profile_id",
        "training_input_hash",
        "training_run_hash",
        "source_identity_hash",
        "model_hashes",
        "content_hash",
    }
    if (
        set(pointer) != expected
        or pointer.get("profile_id") != profile.profile_id
        or not isinstance(model_hashes, list)
        or len(model_hashes) != 2
        or any(not isinstance(value, str) or len(value) != 64 for value in model_hashes)
        or pointer.get("content_hash")
        != ContentHash({key: value for key, value in pointer.items() if key != "content_hash"})
    ):
        raise ValueError("trained profile pointer is invalid")
    heads = tuple(
        InspectActiveHeadBundle(profile_root / contract.directory_name, contract.strategy, profile)
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


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InspectActiveProfileBundles", "PublishProfileBundles"]
