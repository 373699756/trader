"""Publish both trained heads in the fixed, portable profile layout."""

from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import LoadHeadBundle, ModelBundleContractError
from trader.training.infra.model_bundles.bundle_snapshot import InspectActiveHeadBundle, LoadedActiveHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

_LOGGER = logging.getLogger(__name__)


class ProfilePublicationBusyError(RuntimeError):
    """The short profile publication boundary could not be entered in time."""


@contextmanager
def _publication_lock(profile_root: Path, timeout_seconds: float) -> Iterator[None]:
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("profile publication timeout must be finite and nonnegative")
    lock = ProcessLock(profile_root.with_name(f".{profile_root.name}.publication.lock"), label="model publication")
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            lock.acquire()
            break
        except ProcessLockError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProfilePublicationBusyError("model_publication_busy") from exc
            time.sleep(min(0.01, remaining))
    try:
        yield
    finally:
        lock.release()


def PublishProfileBundles(
    profile_root: Path,
    profile: TrainedProfileContract,
    staged: tuple[tuple[Strategy, Path], ...],
    *,
    timeout_seconds: float = 2.0,
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
        with _publication_lock(profile_root, timeout_seconds):
            if profile_root.is_symlink() or (profile_root.exists() and not profile_root.is_dir()):
                raise ValueError("trained profile directory is invalid")
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
        return tuple(replace(head, model_path=profile_root / head.strategy.value / "model.json") for head in heads)
    finally:
        if publication.exists():
            shutil.rmtree(publication)


def InspectActiveProfileBundles(
    profile_root: Path, profile: TrainedProfileContract, *, timeout_seconds: float = 2.0
) -> tuple[LoadedActiveHeadBundle, ...]:
    """Validate the two fixed heads from one selected profile directory."""

    # A publisher creates this parent before entering its lock. An absent parent
    # is an empty installation, not the temporary gap inside a publication.
    if not profile_root.parent.exists():
        raise FileNotFoundError(profile_root)
    with _publication_lock(profile_root, timeout_seconds):
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
        raise ModelBundleContractError("trained profile pair identity is inconsistent")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["InspectActiveProfileBundles", "ProfilePublicationBusyError", "PublishProfileBundles"]
