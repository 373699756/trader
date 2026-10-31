"""One disposable, verified complete sample cache per locked training profile."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import errno
import hashlib
import logging
import os
import shutil
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
        progress: SampleCacheProgress | None = None, sqlite_policy: SqliteIoPolicy,
    ) -> None:
        self._sqlite_policy = sqlite_policy
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
        self._inspected = False
        self._inspection_signature: tuple[tuple[int, int, int, int] | None, ...] = ()

    @property
    def feature_count(self) -> int:
        return len(self.feature_names)

    def inspect(self) -> bool:
        if self.root.is_symlink() or self.path.is_symlink() or self.manifest.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        signature = self._signature()
        if not self._inspected or signature != self._inspection_signature:
            self.reused = self._matches()
            self._inspected = True
            self._inspection_signature = signature
        return self.reused

    def open(self, *, inspected: bool = False) -> SQLiteTrainingSampleIndex:
        if self.root.is_symlink() or self.path.is_symlink() or self.manifest.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        # The caller holds the profile lock; these are scratch mappings from a
        # killed fit, never sealed samples or another running profile's files.
        for pattern in ("sample-fit-*", "sample-shared-*"):
            for workspace in self.root.glob(pattern):
                if workspace.is_symlink():
                    raise ValueError("training_sample_cache_symlink")
                if workspace.is_dir():
                    shutil.rmtree(workspace)
        if not inspected:
            self.inspect()
        if not self.reused:
            self.clear()
            self.root.mkdir(parents=True, exist_ok=True)
        return SQLiteTrainingSampleIndex(
            self.path,
            self.feature_names,
            prepared_split_hash=self.split_hash if self.reused else None,
            sequence_layout=self.sequence_layout, sqlite_policy=self._sqlite_policy,
        )

    def seal(self, samples: SQLiteTrainingSampleIndex) -> None:
        samples.require_split_hash(self.split_hash)
        before = self.path.stat()
        size = before.st_size
        if self._progress:
            self._progress("sealing", "started", 0, size)
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())
        digest = _digest(self.path, self._checkpoint, self._report_bytes("sealing"))
        stamp = self.path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns):
            raise ValueError("training_sample_cache_changed_during_seal")
        AtomicWriteJson(self.manifest, {
            "identity": self.identity, "sha256": digest, "storage": self.storage_contract, "split_hash": self.split_hash,
            "size_bytes": stamp.st_size, "modified_ns": stamp.st_mtime_ns,
        })
        if self._progress:
            self._progress("sealing", "completed", size, size)
        self._inspected = False

    def _signature(self) -> tuple[tuple[int, int, int, int] | None, ...]:
        result: list[tuple[int, int, int, int] | None] = []
        for path in (self.path, self.manifest):
            try:
                value = path.stat()
            except FileNotFoundError:
                result.append(None)
            else:
                result.append((value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns))
        return tuple(result)

    def _report_bytes(self, phase: SampleCachePhase) -> Callable[[int, int], None]:
        def report(completed: int, total: int) -> None:
            if self._progress:
                self._progress(phase, "running", completed, total)

        return report

    def audit(self) -> bool:
        """Explicit full-byte verification; normal reuse checks the sealed metadata."""
        if self.root.is_symlink() or self.path.is_symlink() or self.manifest.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        return self._matches(verify_content=True)

    def _matches(self, *, verify_content: bool = False) -> bool:
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
            stamp = self.path.stat()
            size = stamp.st_size
            if (
                value.get("size_bytes") != size
                or value.get("modified_ns") != stamp.st_mtime_ns
                or not isinstance(value.get("sha256"), str)
                or len(value["sha256"]) != 64
            ):
                return False
            units = size if verify_content else 1
            if self._progress:
                self._progress("checking", "started", 0, units)
            if self._checkpoint is not None:
                self._checkpoint()
            matches = not verify_content or value["sha256"] == _digest(
                self.path, self._checkpoint, self._report_bytes("checking")
            )
            if self._progress:
                self._progress("checking", "completed", units, units)
            return matches
        except InterruptedError:
            raise
        except (OSError, ValueError):
            _LOGGER.warning("training_sample_cache_invalid_rebuilding")
            return False

    def clear(self) -> None:
        if self.root.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        self.reused = False
        self._inspected = False
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
