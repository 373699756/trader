"""Runtime cache of verified file generations; published receipts remain authoritative."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import threading
from typing import cast

from trader.infra.atomic_files import AtomicWriteJson
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_publication import (
    SqliteFileStamp, SqlitePublicationError, SqlitePublicationFingerprint, VerifiedSqlitePublication,
)

_MAX_CACHE_BYTES = 8192
_LOGGER = logging.getLogger(__name__)


def _stamp(value: os.stat_result) -> SqliteFileStamp:
    return SqliteFileStamp(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _stamp_document(value: SqliteFileStamp | None) -> list[int] | None:
    return None if value is None else [value.device, value.inode, value.size, value.modified_ns, value.changed_ns]


def _decode_stamp(value: object) -> SqliteFileStamp:
    if not isinstance(value, list) or len(value) != 5 or any(type(item) is not int or item < 0 for item in value):
        raise SqlitePublicationError("sqlite_verification_cache_invalid")
    return SqliteFileStamp(*cast(list[int], value))


def _digest(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise SqlitePublicationError("sqlite_verification_cache_invalid")
    return value


def _header(value: object, size: int) -> bytes:
    if not isinstance(value, str) or len(value) != size * 2:
        raise SqlitePublicationError("sqlite_verification_cache_invalid")
    try:
        return bytes.fromhex(value)
    except ValueError as exc:
        raise SqlitePublicationError("sqlite_verification_cache_invalid") from exc


def _document(proof: VerifiedSqlitePublication) -> list[object]:
    fingerprint = proof.fingerprint
    return [
        _stamp_document(proof.database), _stamp_document(proof.wal), _stamp_document(proof.receipt),
        [fingerprint.database_size, fingerprint.database_sha256, fingerprint.wal_size, fingerprint.wal_sha256],
        proof.database_header.hex(), proof.wal_header.hex() if proof.wal_header is not None else None,
    ]


def _decode(value: object) -> VerifiedSqlitePublication:
    if not isinstance(value, list) or len(value) != 6:
        raise SqlitePublicationError("sqlite_verification_cache_invalid")
    database, wal, receipt = _decode_stamp(value[0]), _decode_stamp(value[1]) if value[1] is not None else None, _decode_stamp(value[2])
    fingerprint = value[3]
    if (
        not isinstance(fingerprint, list) or len(fingerprint) != 4
        or type(fingerprint[0]) is not int or type(fingerprint[2]) is not int
        or fingerprint[0] != database.size or fingerprint[2] != (wal.size if wal else 0)
    ):
        raise SqlitePublicationError("sqlite_verification_cache_invalid")
    result = VerifiedSqlitePublication(
        database, wal, receipt,
        SqlitePublicationFingerprint(database.size, _digest(fingerprint[1]), wal.size if wal else 0,
                                     _digest(fingerprint[3]) if fingerprint[3] is not None else None),
        _header(value[4], 100), _header(value[5], 32) if value[5] is not None else None,
        sqlite_readability_verified=False,
    )
    if (wal is None) != (result.fingerprint.wal_sha256 is None) or (wal is None) != (result.wal_header is None):
        raise SqlitePublicationError("sqlite_verification_cache_invalid")
    return result


class SqliteVerificationCache:
    """Explicitly injected runtime directory; construction performs no file I/O."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._lock = threading.RLock()
        self._memory: dict[str, tuple[SqliteFileStamp | None, VerifiedSqlitePublication]] = {}
        self._write_failures: set[str] = set()

    def _path(self, source: Path) -> tuple[str, Path]:
        key = str(source.resolve())
        identity = hashlib.sha256(key.encode()).hexdigest()[:24]
        path = self.directory / f"{source.name}.{identity}.json"
        if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
            raise SqlitePublicationError("sqlite_verification_cache_unsafe")
        return key, path

    def load(self, source: Path) -> VerifiedSqlitePublication | None:
        with self._lock:
            key, path = self._path(source)
            cached = self._memory.get(key)
            try:
                current = _stamp(path.stat())
            except FileNotFoundError:
                return cached[1] if cached is not None else None
            except OSError:
                if cached is not None:
                    return cached[1]
                raise
            if cached is not None and cached[0] == current:
                return cached[1]
            if current.size > _MAX_CACHE_BYTES or not path.is_file():
                raise SqlitePublicationError("sqlite_verification_cache_invalid")
            with path.open("rb") as stream:
                current = _stamp(os.fstat(stream.fileno()))
                payload = stream.read(_MAX_CACHE_BYTES + 1)
            if len(payload) > _MAX_CACHE_BYTES:
                raise SqlitePublicationError("sqlite_verification_cache_invalid")
            try:
                document = json.loads(payload)
                if not isinstance(document, dict) or set(document) != {"source", "proof", "content_hash"} or document["source"] != key:
                    raise SqlitePublicationError("sqlite_verification_cache_invalid")
                if document["content_hash"] != CanonicalDigest((key, document["proof"])):
                    raise SqlitePublicationError("sqlite_verification_cache_invalid")
                proof = _decode(document["proof"])
            except (ValueError, TypeError, RecursionError) as exc:
                raise SqlitePublicationError("sqlite_verification_cache_invalid") from exc
            self._memory[key] = (current, proof)
            return proof

    def remember(self, source: Path, proof: VerifiedSqlitePublication) -> None:
        with self._lock:
            key, path = self._path(source)
            document = _document(proof)
            cached = self._memory.get(key)
            try:
                if cached is not None and _document(cached[1]) == document and path.is_file() and _stamp(path.stat()) == cached[0]:
                    self._memory[key] = (cached[0], proof)
                    return
                AtomicWriteJson(path, {"source": key, "proof": document, "content_hash": CanonicalDigest((key, document))})
                current = _stamp(path.stat())
            except OSError as exc:
                self._memory[key] = (None, proof)
                if key not in self._write_failures:
                    self._write_failures.add(key)
                    _LOGGER.warning("sqlite_verification_cache_write_failed file=%s category=%s; verified proof retained in memory", source.name, type(exc).__name__)
                return
            self._memory[key] = (current, proof)
            self._write_failures.discard(key)
