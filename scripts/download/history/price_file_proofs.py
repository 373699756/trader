"""Persistent file digests for explicit, external price-audit checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import re
import json
from typing import cast

from trader.infra.atomic_files import AtomicReadJson, AtomicWriteJson
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_policy import SqliteIoPolicy
from trader.infra.sqlite_publication import (
    SqliteFileStamp,
    SqlitePublicationFingerprint,
    VerifiedSqlitePublication,
    VerifySqliteFile,
)

_MAX_BYTES = 2 * 1024 * 1024
_MAX_FILES = 256


def PriceAuditProofPath(checkpoint: Path) -> Path:
    return checkpoint.with_name(checkpoint.name + ".hashes.json")


@dataclass(frozen=True)
class AuditFileDigest:
    path: str
    stamp: SqliteFileStamp
    sha256: str
    header: bytes


def _safe_path(path: Path) -> None:
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError("price_audit_hash_cache_unsafe")


def _stamp(path: Path) -> SqliteFileStamp:
    _safe_path(path)
    if not path.is_file():
        raise ValueError("price_audit_source_missing_or_unsafe")
    value = path.stat()
    return SqliteFileStamp(value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _header(path: Path) -> bytes:
    with path.open("rb") as source:
        return source.read(32)


def _items(value: object, size: int) -> list[object]:
    if not isinstance(value, list) or len(value) != size:
        raise ValueError("price_audit_hash_cache_invalid")
    return cast(list[object], value)


def _integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2**64 - 1:
        raise ValueError("price_audit_hash_cache_invalid")
    return value


def _decode_stamp(value: object) -> SqliteFileStamp:
    return SqliteFileStamp(*(_integer(item) for item in _items(value, 5)))


def _stamp_document(value: SqliteFileStamp | None) -> list[int] | None:
    return None if value is None else [value.device, value.inode, value.size, value.modified_ns, value.changed_ns]


def _sha256(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("price_audit_hash_cache_invalid")
    return value


def _decode_header(value: object, size: int, *, exact: bool = True) -> bytes:
    if not isinstance(value, str):
        raise ValueError("price_audit_hash_cache_invalid")
    try:
        header = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("price_audit_hash_cache_invalid") from exc
    if (exact and len(header) != size) or (not exact and len(header) > size):
        raise ValueError("price_audit_hash_cache_invalid")
    return header


def _path_key(value: object) -> str:
    if not isinstance(value, str) or len(value) > 4096 or not Path(value).is_absolute():
        raise ValueError("price_audit_hash_cache_invalid")
    return value


def _publication_document(path: str, value: VerifiedSqlitePublication) -> list[object]:
    fingerprint = value.fingerprint
    return [
        path, _stamp_document(value.database), _stamp_document(value.wal), _stamp_document(value.receipt),
        [fingerprint.database_size, fingerprint.database_sha256, fingerprint.wal_size, fingerprint.wal_sha256],
        value.database_header.hex(), value.wal_header.hex() if value.wal_header is not None else None,
    ]


def _decode_publication(value: object) -> tuple[str, VerifiedSqlitePublication]:
    row = _items(value, 7)
    database = _decode_stamp(row[1])
    wal = _decode_stamp(row[2]) if row[2] is not None else None
    fingerprint = _items(row[4], 4)
    result = VerifiedSqlitePublication(
        database, wal, _decode_stamp(row[3]),
        SqlitePublicationFingerprint(
            _integer(fingerprint[0]), _sha256(fingerprint[1]), _integer(fingerprint[2]),
            _sha256(fingerprint[3]) if fingerprint[3] is not None else None,
        ),
        _decode_header(row[5], 100), _decode_header(row[6], 32) if row[6] is not None else None,
        sqlite_readability_verified=False,
    )
    if (
        result.fingerprint.database_size != database.size
        or result.fingerprint.wal_size != (wal.size if wal is not None else 0)
        or (wal is None) != (result.fingerprint.wal_sha256 is None)
        or (wal is None) != (result.wal_header is None)
    ):
        raise ValueError("price_audit_hash_cache_invalid")
    return _path_key(row[0]), result


class PriceAuditFileProofs:
    """Cache digests only; field scans and their checkpoint remain separate evidence."""

    def __init__(self, path: Path | None, check: Callable[[], None]) -> None:
        self._path = path
        self._check = check
        self._digests: dict[str, AuditFileDigest] = {}
        self._publications: dict[str, VerifiedSqlitePublication] = {}
        self._dirty = False
        self.reused_files = 0
        self.hashed_files = 0
        if path is not None:
            _safe_path(path)
            if path.exists():
                self._load(path)

    def _load(self, path: Path) -> None:
        self._check()
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            raise ValueError("price_audit_hash_cache_invalid")
        value = AtomicReadJson(path)
        if not isinstance(value, dict) or set(value) != {"digests", "publications", "content_hash"}:
            raise ValueError("price_audit_hash_cache_invalid")
        digests, publications = value["digests"], value["publications"]
        if any(not isinstance(items, list) or len(items) > _MAX_FILES for items in (digests, publications)):
            raise ValueError("price_audit_hash_cache_invalid")
        if value["content_hash"] != CanonicalDigest((digests, publications)):
            raise ValueError("price_audit_hash_cache_invalid")
        for entry in cast(list[object], digests):
            self._check()
            row = _items(entry, 4)
            key = _path_key(row[0])
            if key in self._digests:
                raise ValueError("price_audit_hash_cache_invalid")
            self._digests[key] = AuditFileDigest(
                key, _decode_stamp(row[1]), _sha256(row[2]), _decode_header(row[3], 32, exact=False),
            )
        for entry in cast(list[object], publications):
            self._check()
            key, proof = _decode_publication(entry)
            if key in self._publications:
                raise ValueError("price_audit_hash_cache_invalid")
            self._publications[key] = proof

    def digest(self, path: Path, compute: Callable[[], str]) -> AuditFileDigest:
        self._check()
        before = _stamp(path)
        header = _header(path)
        key = str(path.resolve())
        previous = self._digests.get(key)
        if previous is not None and previous.stamp == before and previous.header == header:
            self.reused_files += 1
            return previous
        result = AuditFileDigest(key, before, _sha256(compute()), header)
        if _stamp(path) != before or _header(path) != header:
            raise ValueError("price_audit_source_changed")
        self._digests[key] = result
        self._dirty = True
        self.hashed_files += 1
        return result

    def verify(self, path: Path, policy: SqliteIoPolicy) -> VerifiedSqlitePublication:
        self._check()
        _safe_path(path)
        key = str(path.resolve())
        previous = self._publications.get(key)
        proof = VerifySqliteFile(path, sqlite_policy=policy, previous=previous, check=self._check, file_only=True)
        if previous is not None and previous.database == proof.database and previous.database_header == proof.database_header:
            self.reused_files += 1
        else:
            self.hashed_files += 1
        if proof != previous:
            self._publications[key] = proof
            self._dirty = True
        self._remember(key, proof.database, proof.fingerprint.database_sha256, proof.database_header[:32])
        if proof.wal is not None and proof.fingerprint.wal_sha256 is not None and proof.wal_header is not None:
            self._remember(str(Path(str(path) + "-wal").resolve()), proof.wal, proof.fingerprint.wal_sha256, proof.wal_header)
        # Commit each annual proof before moving to the next database or field scan.
        self.flush()
        return proof

    def _remember(self, key: str, stamp: SqliteFileStamp, digest: str, header: bytes) -> None:
        proof = AuditFileDigest(key, stamp, digest, header)
        if self._digests.get(key) != proof:
            self._digests[key] = proof
            self._dirty = True

    def flush(self) -> None:
        if self._path is None or not self._dirty:
            return
        self._check()
        _safe_path(self._path)
        if len(self._digests) > _MAX_FILES or len(self._publications) > _MAX_FILES:
            raise ValueError("price_audit_hash_cache_size_limit")
        digests: list[object] = [
            [key, _stamp_document(proof.stamp), proof.sha256, proof.header.hex()]
            for key, proof in sorted(self._digests.items())
        ]
        publications: list[object] = [_publication_document(key, proof) for key, proof in sorted(self._publications.items())]
        document = {"digests": digests, "publications": publications, "content_hash": CanonicalDigest((digests, publications))}
        if len(json.dumps(document, separators=(",", ":")).encode()) > _MAX_BYTES:
            raise ValueError("price_audit_hash_cache_size_limit")
        AtomicWriteJson(self._path, document)
        self._dirty = False
        self._check()
