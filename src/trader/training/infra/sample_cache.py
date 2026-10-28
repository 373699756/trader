"""One disposable, verified complete sample cache per locked training profile."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson
from trader.training.infra.sample_index import SQLiteTrainingSampleIndex
from trader.training.infra.sample_sequences import SampleSequenceLayout
from trader.training.infra.sample_schema import SampleStorageId

_LOGGER = logging.getLogger(__name__)
SampleCachePhase = Literal["checking", "sealing"]
SampleCacheProgress = Callable[[SampleCachePhase, Literal["started", "running", "completed"], int, int], None]


class TrainingSampleCache:
    """Caller holds the profile lock; incomplete or different inputs are rebuilt."""

    def __init__(
        self,
        root: Path,
        identity: str,
        feature_names: tuple[str, ...],
        split_hash: str,
        checkpoint: Callable[[], None] | None = None,
        *,
        sequence_layout: SampleSequenceLayout | None = None,
        progress: SampleCacheProgress | None = None,
    ) -> None:
        self.root = root
        self.path = self.root / "samples.sqlite3"
        self.manifest = self.root / "complete.json"
        self.identity = identity
        self.feature_names = feature_names
        self.split_hash = split_hash
        self.sequence_layout = sequence_layout
        self.storage_contract = SampleStorageId(feature_names, sequence_layout)
        self.reused = False
        self._checkpoint = checkpoint
        self._progress = progress

    @property
    def feature_count(self) -> int:
        return len(self.feature_names)

    def inspect(self) -> bool:
        if self.root.is_symlink() or self.path.is_symlink() or self.manifest.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        self.reused = self._matches()
        return self.reused

    def open(self, *, inspected: bool = False) -> SQLiteTrainingSampleIndex:
        if self.root.is_symlink() or self.path.is_symlink() or self.manifest.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        if not inspected:
            self.inspect()
        if not self.reused:
            self.clear()
            self.root.mkdir(parents=True, exist_ok=True)
        return SQLiteTrainingSampleIndex(
            self.path,
            self.feature_names,
            prepared_split_hash=self.split_hash if self.reused else None,
            sequence_layout=self.sequence_layout,
        )

    def seal(self, samples: SQLiteTrainingSampleIndex) -> None:
        samples.require_split_hash(self.split_hash)
        size = self.path.stat().st_size
        if self._progress:
            self._progress("sealing", "started", 0, size)
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())
        digest = _digest(self.path, self._checkpoint, self._report_bytes("sealing"))
        AtomicWriteJson(self.manifest, {
            "identity": self.identity, "sha256": digest, "storage": self.storage_contract, "split_hash": self.split_hash
        })
        if self._progress:
            self._progress("sealing", "completed", size, size)

    def _report_bytes(self, phase: SampleCachePhase) -> Callable[[int, int], None]:
        def report(completed: int, total: int) -> None:
            if self._progress:
                self._progress(phase, "running", completed, total)

        return report

    def _matches(self) -> bool:
        if not self.manifest.is_file() or not self.path.is_file():
            return False
        try:
            value = AtomicReadJson(self.manifest)
            if (
                not isinstance(value, dict)
                or value.get("identity") != self.identity
                or value.get("storage") != self.storage_contract
                or value.get("split_hash") != self.split_hash
            ):
                return False
            size = self.path.stat().st_size
            if self._progress:
                self._progress("checking", "started", 0, size)
            digest = _digest(self.path, self._checkpoint, self._report_bytes("checking"))
            if self._progress:
                self._progress("checking", "completed", size, size)
            return value.get("sha256") == digest
        except InterruptedError:
            raise
        except (OSError, ValueError):
            _LOGGER.warning("training_sample_cache_invalid_rebuilding")
            return False

    def clear(self) -> None:
        if self.root.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        # Readiness goes first: interruption must never bless partial bytes.
        for path in (self.manifest, self.path, Path(f"{self.path}-journal")):
            if path.is_symlink():
                raise ValueError("training_sample_cache_symlink")
            path.unlink(missing_ok=True)


@contextmanager
def TemporaryTrainingSamples(samples_root: Path, profile: str) -> Iterator[Path]:
    """Keep one profile's temporary samples only while its training run is active."""
    if profile not in ("v1", "v2", "v3"):
        raise ValueError("training sample profile is invalid")
    profile_root = samples_root / profile
    if profile_root.is_symlink() or (
        profile_root.exists() and (not profile_root.is_dir() or any(profile_root.iterdir()))
    ):
        raise ValueError("training sample directory contains existing data")
    profile_root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="training-", dir=profile_root) as temporary:
            yield Path(temporary)
    finally:
        PruneEmptySampleDirectories(profile_root, samples_root)


def PruneEmptySampleDirectories(profile_root: Path, samples_root: Path) -> None:
    """Remove empty profile and parent directories without touching retained caches."""
    if profile_root.parent != samples_root:
        raise ValueError("training sample profile is outside the sample root")
    for directory in (profile_root, samples_root):
        try:
            directory.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                _LOGGER.warning("training_sample_directory_cleanup_failed", exc_info=True)
            break


def SampleCacheBytes(cache: TrainingSampleCache | None) -> int:
    if cache is None:
        return 0
    try:
        return cache.path.stat().st_size
    except FileNotFoundError:
        return 0


def _digest(
    path: Path,
    checkpoint: Callable[[], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> str:
    digest = hashlib.sha256()
    total = path.stat().st_size
    completed = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            if checkpoint is not None:
                checkpoint()
            digest.update(chunk)
            completed += len(chunk)
            if completed > total:
                raise ValueError("training_sample_cache_changed_during_hash")
            if progress is not None:
                progress(completed, total)
    if completed != total:
        raise ValueError("training_sample_cache_changed_during_hash")
    return digest.hexdigest()
