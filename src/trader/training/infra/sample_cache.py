"""One disposable, verified complete sample cache per locked training profile."""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

from trader.infra.atomic_files.json import AtomicReadJson, AtomicWriteJson
from trader.training.infra.sample_repository import SQLiteTrainingSampleRepository

_LOGGER = logging.getLogger(__name__)


class TrainingSampleCache:
    """Caller holds the profile lock; incomplete or different inputs are rebuilt."""

    def __init__(self, root: Path, identity: str, feature_count: int, split_hash: str) -> None:
        self.root = root / ".sample-cache"
        self.path = self.root / "samples.sqlite3"
        self.manifest = self.root / "complete.json"
        self.identity = identity
        self.feature_count = feature_count
        self.split_hash = split_hash
        self.reused = False

    def open(self) -> SQLiteTrainingSampleRepository:
        if self.root.is_symlink() or self.path.is_symlink() or self.manifest.is_symlink():
            raise ValueError("training_sample_cache_symlink")
        self.reused = self._matches()
        if not self.reused:
            self.clear()
            self.root.mkdir(parents=True, exist_ok=True)
        return SQLiteTrainingSampleRepository(
            self.path, self.feature_count, prepared_split_hash=self.split_hash if self.reused else None
        )

    def seal(self, samples: SQLiteTrainingSampleRepository) -> None:
        samples.require_split_hash(self.split_hash)
        with self.path.open("rb") as handle:
            os.fsync(handle.fileno())
        AtomicWriteJson(self.manifest, {"identity": self.identity, "sha256": _digest(self.path)})

    def _matches(self) -> bool:
        if not self.manifest.is_file() or not self.path.is_file():
            return False
        try:
            value = AtomicReadJson(self.manifest)
            return (
                isinstance(value, dict)
                and value.get("identity") == self.identity
                and value.get("sha256") == _digest(self.path)
            )
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


def SampleCacheBytes(cache: TrainingSampleCache | None) -> int:
    if cache is None:
        return 0
    try:
        return cache.path.stat().st_size
    except FileNotFoundError:
        return 0


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
