"""Publish both trained heads in the fixed, portable profile layout."""

from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.recommendation.domain.publication.models import Strategy
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, TrainedHeadModelBundle
from trader.training.infra.model_bundles.bundle_snapshot import InspectPublishedHeadBundle, LoadedPublishedHeadBundle
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

_LOGGER = logging.getLogger(__name__)


class ProfilePublicationBusyError(RuntimeError):
    """The short profile publication boundary could not be entered in time."""


@dataclass(frozen=True)
class _ProfileWorkspace:
    has_pair: bool
    staging: tuple[Path, ...]


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
    if not profile_root.parent.exists():
        return
    with ProfilePublicationLock(profile_root, timeout_seconds):
        if profile_root.exists() or profile_root.is_symlink():
            _inspect_owned_workspace(profile_root, allow_partial_heads=True)
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
        workspace = _inspect_owned_workspace(candidate)
        if workspace.has_pair:
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


def ClearProfileStaging(profile_root: Path, *, timeout_seconds: float = 2.0) -> None:
    """Remove only current owned staging after validating the entire profile tree."""
    with ProfilePublicationLock(profile_root, timeout_seconds):
        workspace = _inspect_owned_workspace(profile_root, allow_partial_heads=True)
        for staging in workspace.staging:
            shutil.rmtree(staging)


def PublishProfileBundles(
    profile_root: Path,
    profile: TrainedProfileContract,
    staged: tuple[tuple[Strategy, Path], ...],
    *,
    timeout_seconds: float = 2.0,
) -> tuple[LoadedPublishedHeadBundle, ...]:
    """Publish a recent production pair after validating both heads and their shared source."""

    if tuple(strategy for strategy, _ in staged) != tuple(head.strategy for head in profile.heads):
        raise ValueError("profile publication requires both ordered strategy heads")
    bundles = tuple(InspectPublishedHeadBundle(path, strategy, profile).model for strategy, path in staged)
    if (
        len({item.training_input_hash for item in bundles}) != 1
        or len({item.training_run_hash for item in bundles}) != 1
        or len({item.source_identity_hash for item in bundles}) != 1
    ):
        raise ValueError("profile publication heads have different source snapshots")
    _require_recent_diagnostics(bundles)
    profile_root.parent.mkdir(parents=True, exist_ok=True)
    RecoverProfileBundles(profile_root, profile, timeout_seconds=timeout_seconds)
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
        _require_recent_diagnostics(head.model for head in heads)
        with ProfilePublicationLock(profile_root, timeout_seconds):
            if profile_root.exists() or profile_root.is_symlink():
                _inspect_owned_workspace(profile_root, allow_partial_heads=True)
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
                _inspect_owned_workspace(previous, allow_partial_heads=True)
                shutil.rmtree(previous)
            except OSError:
                _LOGGER.warning("trained_profile_previous_cleanup_failed")
        return tuple(replace(head, model_path=profile_root / head.strategy.value / "model.json") for head in heads)
    finally:
        if publication.exists() or publication.is_symlink():
            _inspect_owned_workspace(publication, allow_partial_heads=True)
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


def _inspect_owned_workspace(profile_root: Path, *, allow_partial_heads: bool = False) -> _ProfileWorkspace:
    # Writers may replace owned incomplete heads; restoring an old generation
    # additionally requires a complete pair and its caller's content validation.
    if profile_root.is_symlink() or not profile_root.is_dir():
        raise ValueError("model publication recovery directory is invalid")
    directories = tuple(profile_root.iterdir())
    heads = {path.name for path in directories} & {"tomorrow", "d25"}
    if not allow_partial_heads and heads and heads != {"tomorrow", "d25"}:
        raise ValueError("model publication recovery directory is missing a head")
    staging = []
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("model publication recovery head is invalid")
        files = {path.name for path in directory.iterdir()}
        if directory.name in heads:
            if not files <= {"model.json", "metadata.json"} or (
                not allow_partial_heads and files != {"model.json", "metadata.json"}
            ):
                raise ValueError("model publication recovery head has unexpected entries")
        elif directory.name.startswith(".bundle-staging."):
            suffix = directory.name.removeprefix(".bundle-staging.")
            if (
                len(suffix) != 8
                or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_" for character in suffix)
                or not files <= {"model.json", "metadata.json"}
            ):
                raise ValueError("model publication recovery staging has unexpected entries")
            staging.append(directory)
        else:
            raise ValueError("model publication recovery head has unexpected entries")
        if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
            raise ValueError("model publication recovery file is invalid")
    # A first installation has only owned staging directories, never a published
    # pair. Restore that workspace so training can rebuild, without reusing a head.
    return _ProfileWorkspace(heads == {"tomorrow", "d25"}, tuple(staging))


def _validate_profile_pair(heads: tuple[LoadedPublishedHeadBundle, ...]) -> None:
    if (
        len(heads) != 2
        or len({item.training_input_hash for item in heads}) != 1
        or len({item.training_run_hash for item in heads}) != 1
        or len({item.source_identity_hash for item in heads}) != 1
    ):
        raise ModelBundleContractError("trained profile pair identity is inconsistent")


def _require_recent_diagnostics(models: Iterable[TrainedHeadModelBundle]) -> None:
    if any(model.diagnostic_plan is None for model in models):
        raise ModelBundleContractError("profile publication requires recent production diagnostics")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ClearProfileStaging",
    "InspectPublishedProfileBundles",
    "ProfilePublicationBusyError",
    "ProfilePublicationLock",
    "PublishProfileBundles",
    "RecoverProfileBundles",
    "RecoverProfilePublication",
]
