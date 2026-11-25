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
from datetime import datetime
from pathlib import Path

from trader.infra.process_lock import ProcessLock, ProcessLockError
from trader.infra.cache_contracts import BoundedCache
from trader.recommendation.domain.publication.models import Strategy
from trader.training.domain.model_publication import ModelPublicationIdentity
from trader.training.infra.model_bundles.bundle_codec import ModelBundleContractError, TrainedHeadModelBundle
from trader.training.infra.model_bundles.bundle_snapshot import (
    InspectPublishedHeadBundle, LoadedPublishedHeadBundle, ReadPublishedHeadIdentity,
)
from trader.training.infra.model_bundles.contracts import TrainedProfileContract

_LOGGER = logging.getLogger(__name__)
_PAIRED_STRATEGIES = (Strategy.TOMORROW, Strategy.D25)
_MODEL_FILES = ("model.json", "metadata.json")


class ProfilePublicationBusyError(RuntimeError):
    """The short profile publication boundary could not be entered in time."""


@dataclass(frozen=True)
class ProfileWorkspace:
    has_pair: bool
    staging: tuple[Path, ...]


@contextmanager
def ProfilePublicationLock(
    profile_root: Path, timeout_seconds: float = 2.0, *, cancel_requested: Callable[[], bool] | None = None
) -> Iterator[None]:
    """Bound a profile reader or publisher's wait on the shared directory lock."""
    if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
        raise ValueError("profile publication timeout must be finite and nonnegative")
    lock = ProcessLock(profile_root.with_name(f".{profile_root.name}.publication.lock"), label="model publication")
    deadline = time.monotonic() + timeout_seconds
    while True:
        if cancel_requested is not None and cancel_requested():
            raise InterruptedError("model_publication_observation_cancelled")
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
    profile_root: Path, validate: Callable[[Path], object], *, timeout_seconds: float = 2.0,
    strategies: tuple[Strategy, ...] = _PAIRED_STRATEGIES,
) -> None:
    """Explicit writers recover a unique validated old pair under the publication lock."""
    if not profile_root.parent.exists():
        return
    with ProfilePublicationLock(profile_root, timeout_seconds):
        if profile_root.exists() or profile_root.is_symlink():
            InspectProfileWorkspace(profile_root, allow_partial_heads=True, strategies=strategies)
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
        workspace = InspectProfileWorkspace(candidate, strategies=strategies)
        if workspace.has_pair:
            validate(candidate)
        os.replace(candidate, profile_root)
        _fsync_directory(profile_root.parent)


def RecoverProfileBundles(
    profile_root: Path, profile: TrainedProfileContract, *, timeout_seconds: float = 2.0
) -> None:
    """Training restores the previous pair before deciding whether it can be reused."""
    RecoverProfilePublication(
        profile_root, lambda path: _inspect_profile_directory(path, profile), timeout_seconds=timeout_seconds,
        strategies=tuple(head.strategy for head in profile.heads),
    )


def ClearProfileStaging(
    profile_root: Path, *, timeout_seconds: float = 2.0,
    strategies: tuple[Strategy, ...] = _PAIRED_STRATEGIES,
) -> None:
    """Remove only current owned staging after validating the entire profile tree."""
    with ProfilePublicationLock(profile_root, timeout_seconds):
        workspace = InspectProfileWorkspace(profile_root, allow_partial_heads=True, strategies=strategies)
        for staging in workspace.staging:
            shutil.rmtree(staging)


def RequireProfilePublicationReady(profile_root: Path) -> None:
    """While holding the publication lock, reject unresolved previous generations."""
    if next(profile_root.parent.glob(f".{profile_root.name}.previous.*"), None) is not None:
        raise ValueError("model publication has an unresolved previous directory")


def PublishStrategyModelFiles(profile_root: Path, strategy: Strategy, model_text: str, metadata_text: str) -> None:
    """Publish two prepared model documents without changing sibling strategies."""
    with tempfile.TemporaryDirectory(prefix="strategy-model-") as stage:
        staged = Path(stage)
        for name, text in zip(_MODEL_FILES, (model_text, metadata_text), strict=True):
            (staged / name).write_text(text, encoding="utf-8")
        PublishStrategyModelDirectory(profile_root, strategy, staged)


def PublishStrategyModelDirectory(
    profile_root: Path,
    strategy: Strategy,
    staged: Path,
    *,
    timeout_seconds: float = 2.0,
    validate: Callable[[Path], object] | None = None,
) -> None:
    """Stage and switch exactly one model and metadata pair under the profile lock."""
    if strategy not in _PAIRED_STRATEGIES:
        raise ValueError("model publication requires a scored strategy")
    _inspect_model_files(staged)
    profile_root.parent.mkdir(parents=True, exist_ok=True)
    prefix = f".{profile_root.name}.{strategy.value}"
    destination = profile_root / strategy.value
    previous = profile_root.parent / f"{prefix}.replaced.{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory(prefix=f"{prefix}.publication.", dir=profile_root.parent) as stage:
        publication = Path(stage)
        _copy_model_files(staged, publication)
        if validate is not None:
            validate(publication)
        with ProfilePublicationLock(profile_root, timeout_seconds):
            if profile_root.is_symlink() or (profile_root.exists() and not profile_root.is_dir()):
                raise ValueError("model publication strategy parent is invalid")
            if not profile_root.exists():
                profile_root.mkdir()
                _fsync_directory(profile_root.parent)
            if destination.exists() or destination.is_symlink():
                _inspect_model_files(destination, allow_incomplete=True)
            if _replace_model_directory(destination, publication, previous):
                try:
                    _inspect_model_files(previous, allow_incomplete=True)
                    shutil.rmtree(previous)
                    _fsync_directory(previous.parent)
                except (OSError, ValueError):
                    _LOGGER.warning("trained_profile_previous_cleanup_failed")


def PublishProfileBundles(
    profile_root: Path,
    profile: TrainedProfileContract,
    staged: tuple[tuple[Strategy, Path], ...],
    *,
    timeout_seconds: float = 2.0,
) -> tuple[LoadedPublishedHeadBundle, ...]:
    """Publish a recent production pair after validating both heads and their shared source."""

    strategies = tuple(head.strategy for head in profile.heads)
    if tuple(strategy for strategy, _ in staged) != strategies:
        raise ValueError("profile publication requires the current strategy heads")
    bundles = tuple(InspectPublishedHeadBundle(path, strategy, profile).model for strategy, path in staged)
    if (
        len({item.training_input_hash for item in bundles}) != 1
        or len({item.training_run_hash for item in bundles}) != 1
        or len({item.source_identity_hash for item in bundles}) != 1
    ):
        raise ValueError("profile publication heads have different source snapshots")
    _require_recent_diagnostics(bundles)
    if len(staged) == 1:
        strategy, source = staged[0]
        loaded = InspectPublishedHeadBundle(source, strategy, profile)
        PublishStrategyModelDirectory(
            profile_root, strategy, source, timeout_seconds=timeout_seconds,
            validate=lambda path: InspectPublishedHeadBundle(path, strategy, profile),
        )
        return (replace(loaded, model_path=profile_root / strategy.value / "model.json"),)
    profile_root.parent.mkdir(parents=True, exist_ok=True)
    RecoverProfileBundles(profile_root, profile, timeout_seconds=timeout_seconds)
    publication = Path(tempfile.mkdtemp(prefix=f".{profile_root.name}.publication.", dir=profile_root.parent))
    previous = profile_root.with_name(f".{profile_root.name}.previous.{uuid.uuid4().hex}")
    old_moved = False
    try:
        for strategy, staging in staged:
            destination = publication / strategy.value
            destination.mkdir()
            _copy_model_files(staging, destination)
        _fsync_directory(publication)
        heads = tuple(
            InspectPublishedHeadBundle(publication / contract.directory_name, contract.strategy, profile)
            for contract in profile.heads
        )
        _validate_profile_pair(heads)
        _require_recent_diagnostics(head.model for head in heads)
        with ProfilePublicationLock(profile_root, timeout_seconds):
            RequireProfilePublicationReady(profile_root)
            if profile_root.exists() or profile_root.is_symlink():
                InspectProfileWorkspace(profile_root, allow_partial_heads=True, strategies=strategies)
            old_moved = _replace_model_directory(profile_root, publication, previous)
        if old_moved:
            try:
                InspectProfileWorkspace(previous, allow_partial_heads=True, strategies=strategies)
                shutil.rmtree(previous)
            except (OSError, ValueError):
                _LOGGER.warning("trained_profile_previous_cleanup_failed")
        return tuple(replace(head, model_path=profile_root / head.strategy.value / "model.json") for head in heads)
    finally:
        if publication.exists() or publication.is_symlink():
            InspectProfileWorkspace(publication, allow_partial_heads=True, strategies=strategies)
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


def ReadPublishedProfileIdentities(
    profile_root: Path,
    profile: TrainedProfileContract,
    *,
    cancel_requested: Callable[[], bool],
    timeout_seconds: float = 1.0,
    metadata_cache: BoundedCache[object] | None = None,
    observed_at: datetime | None = None,
) -> tuple[ModelPublicationIdentity, ...]:
    """Observe a pair's declared metadata identity without reading model payloads."""
    if cancel_requested():
        raise InterruptedError("model_publication_observation_cancelled")
    if not math.isfinite(timeout_seconds) or not 0.0 <= timeout_seconds <= 1.0:
        raise ValueError("model publication observation timeout is invalid")
    if not profile_root.parent.exists():
        raise FileNotFoundError(profile_root)
    with ProfilePublicationLock(profile_root, timeout_seconds, cancel_requested=cancel_requested):
        if not profile_root.exists() and not profile_root.is_symlink():
            raise FileNotFoundError(profile_root)
        if profile_root.is_symlink() or not profile_root.is_dir():
            raise ValueError("trained profile directory is invalid")
        identities = tuple(
            ReadPublishedHeadIdentity(
                profile_root / contract.directory_name,
                contract.strategy,
                profile,
                cancel_requested=cancel_requested,
                metadata_cache=metadata_cache, observed_at=observed_at,
            )
            for contract in profile.heads
        )
        if (
            len({item.training_history_hash for item in identities}) != 1
            or len({item.training_run_hash for item in identities}) != 1
        ):
            raise ModelBundleContractError("trained profile publication metadata identity is inconsistent")
        return identities


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
    _require_recent_diagnostics(head.model for head in heads)
    return heads


def _replace_model_directory(destination: Path, publication: Path, previous: Path) -> bool:
    old_moved = destination.exists() or destination.is_symlink()
    if old_moved:
        os.replace(destination, previous)
    new_moved = False
    try:
        os.replace(publication, destination)
        new_moved = True
        _fsync_directory(destination.parent)
        if previous.parent != destination.parent:
            _fsync_directory(previous.parent)
    except OSError:
        if new_moved:
            os.replace(destination, publication)
        if old_moved:
            os.replace(previous, destination)
        raise
    return old_moved


def _inspect_model_files(directory: Path, *, allow_incomplete: bool = False) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("model publication head directory is invalid")
    entries = tuple(directory.iterdir())
    names = {path.name for path in entries}
    expected = set(_MODEL_FILES)
    if not names <= expected or (not allow_incomplete and names != expected):
        raise ValueError("model publication head has unexpected entries")
    if any(path.is_symlink() or not path.is_file() for path in entries):
        raise ValueError("model publication head file is invalid")


def _copy_model_files(source: Path, destination: Path) -> None:
    for name in _MODEL_FILES:
        target = destination / name
        shutil.copyfile(source / name, target)
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
    _fsync_directory(destination)


def InspectProfileWorkspace(
    profile_root: Path, *, allow_partial_heads: bool = False,
    strategies: tuple[Strategy, ...] = _PAIRED_STRATEGIES,
) -> ProfileWorkspace:
    """Reject any unowned entry before a publisher moves or removes a whole tree."""
    # Writers may replace owned incomplete heads; restoring an old generation
    # additionally requires a complete pair and its caller's content validation.
    if profile_root.is_symlink() or not profile_root.is_dir():
        raise ValueError("model publication recovery directory is invalid")
    directories = tuple(profile_root.iterdir())
    required = {strategy.value for strategy in strategies}
    heads = {path.name for path in directories} & required
    if not allow_partial_heads and heads and heads != required:
        raise ValueError("model publication recovery directory is missing a head")
    staging = []
    for directory in directories:
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("model publication recovery head is invalid")
        files = {path.name for path in directory.iterdir()}
        if directory.name in {strategy.value for strategy in _PAIRED_STRATEGIES}:
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
    return ProfileWorkspace(heads == required, tuple(staging))


def _validate_profile_pair(heads: tuple[LoadedPublishedHeadBundle, ...]) -> None:
    if (
        len({item.training_input_hash for item in heads}) != 1
        or len({item.training_run_hash for item in heads}) != 1
        or len({item.source_identity_hash for item in heads}) != 1
    ):
        raise ModelBundleContractError("trained profile pair identity is inconsistent")


def _require_recent_diagnostics(models: Iterable[TrainedHeadModelBundle]) -> None:
    if any(model.final_fit is None or model.diagnostic_plan is None for model in models):
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
    "ReadPublishedProfileIdentities",
    "InspectProfileWorkspace",
    "ProfilePublicationBusyError",
    "ProfilePublicationLock",
    "ProfileWorkspace",
    "PublishProfileBundles",
    "PublishStrategyModelDirectory",
    "PublishStrategyModelFiles",
    "RecoverProfileBundles",
    "RecoverProfilePublication",
    "RequireProfilePublicationReady",
]
