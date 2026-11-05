"""Publish both trained heads in the fixed, portable profile layout."""

from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import LoadHeadBundle, ModelBundleContractError
from trader.training.infra.model_bundles.bundle_snapshot import InspectPublishedHeadBundle, LoadedPublishedHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

_LOGGER = logging.getLogger(__name__)


class ProfilePublicationBusyError(RuntimeError):
    """The short profile publication boundary could not be entered in time."""


@contextmanager
def ProfilePublicationLock(profile_root: Path, timeout_seconds: float = 2.0) -> Iterator[None]:
    """Bound a profile reader or publisher's wait on the shared directory lock."""
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


def RecoverProfilePublication(
    profile_root: Path, validate: Callable[[Path], object], *, timeout_seconds: float = 2.0
) -> None:
    """Explicit writers recover a unique validated old pair under the publication lock."""
    if not profile_root.parent.exists() or profile_root.exists() or profile_root.is_symlink():
        return
    with ProfilePublicationLock(profile_root, timeout_seconds):
        if profile_root.exists() or profile_root.is_symlink():
            return
        previous = tuple(profile_root.parent.glob(f".{profile_root.name}.previous.*"))
        if not previous:
            return
        if len(previous) != 1:
            raise ValueError("model publication recovery has ambiguous previous directories")
        candidate = previous[0]
        suffix = candidate.name.removeprefix(f".{profile_root.name}.previous.")
        if len(suffix) != 32 or any(character not in "0123456789abcdef" for character in suffix):
            raise ValueError("model publication recovery has an unrecognized previous directory")
        has_pair = _require_recoverable_directory(candidate)
        if has_pair:
            validate(candidate)
        os.replace(candidate, profile_root)
        _fsync_directory(profile_root.parent)


def RecoverProfileBundles(
    profile_root: Path, profile: TrainedProfileContract, *, timeout_seconds: float = 2.0
) -> None:
    """Training restores the previous pair before deciding whether it can be reused."""
    RecoverProfilePublication(
        profile_root, lambda path: _inspect_profile_directory(path, profile), timeout_seconds=timeout_seconds
    )


def PublishProfileBundles(
    profile_root: Path,
    profile: TrainedProfileContract,
    staged: tuple[tuple[Strategy, Path], ...],
    *,
    timeout_seconds: float = 2.0,
) -> tuple[LoadedPublishedHeadBundle, ...]:
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
    RecoverProfileBundles(profile_root, profile, timeout_seconds=timeout_seconds)
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
            InspectPublishedHeadBundle(publication / contract.directory_name, contract.strategy, profile)
            for contract in profile.heads
        )
        _validate_profile_pair(heads)
        with ProfilePublicationLock(profile_root, timeout_seconds):
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


def InspectPublishedProfileBundles(
    profile_root: Path, profile: TrainedProfileContract, *, timeout_seconds: float = 2.0
) -> tuple[LoadedPublishedHeadBundle, ...]:
    """Validate the two fixed heads from one selected profile directory."""

    # A publisher creates this parent before entering its lock. An absent parent
    # is an empty installation, not the temporary gap inside a publication.
    if not profile_root.parent.exists():
        raise FileNotFoundError(profile_root)
    with ProfilePublicationLock(profile_root, timeout_seconds):
        return _inspect_profile_directory(profile_root, profile)


def _inspect_profile_directory(
    profile_root: Path, profile: TrainedProfileContract
) -> tuple[LoadedPublishedHeadBundle, ...]:
    if not profile_root.exists() and not profile_root.is_symlink():
        raise FileNotFoundError(profile_root)
    if not profile_root.is_dir() or profile_root.is_symlink():
        raise ValueError("trained profile directory is invalid")
    heads = tuple(
        InspectPublishedHeadBundle(profile_root / contract.directory_name, contract.strategy, profile)
        for contract in profile.heads
    )
    _validate_profile_pair(heads)
    return heads


def _require_recoverable_directory(profile_root: Path) -> bool:
    if profile_root.is_symlink() or not profile_root.is_dir():
        raise ValueError("model publication recovery directory is invalid")
    heads = {path.name for path in profile_root.iterdir()} & {"tomorrow", "d25"}
    if heads and heads != {"tomorrow", "d25"}:
        raise ValueError("model publication recovery directory is missing a head")
    for directory in profile_root.iterdir():
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("model publication recovery head is invalid")
        files = {path.name for path in directory.iterdir()}
        if directory.name in heads:
            if files != {"model.json", "metadata.json"}:
                raise ValueError("model publication recovery head has unexpected entries")
        elif directory.name.startswith(".bundle-staging."):
            suffix = directory.name.removeprefix(".bundle-staging.")
            if (
                len(suffix) != 8
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in suffix)
                or not files <= {"model.json", "metadata.json"}
            ):
                raise ValueError("model publication recovery staging has unexpected entries")
        else:
            raise ValueError("model publication recovery head has unexpected entries")
        if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
            raise ValueError("model publication recovery file is invalid")
    # A first installation has only owned staging directories, never a published
    # pair. Restore that workspace so training can rebuild, without reusing a head.
    return bool(heads)


def _validate_profile_pair(heads: tuple[LoadedPublishedHeadBundle, ...]) -> None:
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


__all__ = [
    "InspectPublishedProfileBundles",
    "ProfilePublicationBusyError",
    "ProfilePublicationLock",
    "PublishProfileBundles",
    "RecoverProfileBundles",
    "RecoverProfilePublication",
]
