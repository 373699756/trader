"""Publish both trained heads in the fixed, portable profile layout."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import LoadHeadBundle
from trader.training.infra.model_bundles.bundle_snapshot import InspectActiveHeadBundle, LoadedActiveHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

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
        for strategy, staging in staged:
            destination = publication / strategy.value
            destination.mkdir()
            for name in ("model.json", "metadata.json"):
                shutil.copyfile(staging / name, destination / name)
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
        _validate_profile_pair(heads)
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
    """Validate the two fixed heads from one selected profile directory."""

    if not profile_root.exists() and not profile_root.is_symlink():
        raise FileNotFoundError(profile_root)
    if not profile_root.is_dir() or profile_root.is_symlink():
        raise ValueError("trained profile directory is invalid")
    heads = tuple(
        InspectActiveHeadBundle(profile_root / contract.directory_name, contract.strategy, profile)
        for contract in profile.heads
    )
    _validate_profile_pair(heads)
    return heads


def _validate_profile_pair(heads: tuple[LoadedActiveHeadBundle, ...]) -> None:
    if (
        len(heads) != 2
        or len({item.training_input_hash for item in heads}) != 1
        or len({item.training_run_hash for item in heads}) != 1
        or len({item.source_identity_hash for item in heads}) != 1
    ):
        raise ValueError("trained profile pair identity is inconsistent")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InspectActiveProfileBundles", "PublishProfileBundles"]
