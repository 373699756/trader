"""Generation-based SQLite increments layered over a sealed BaoStock parent."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import cast

from trader.domain.research.baostock_active_archive import (
    BAOSTOCK_ARCHIVE_FIELD_FAMILIES,
    BaoStockActiveArchiveContext,
    BaoStockActiveManifest,
    BaoStockArchiveFieldFamily,
    BaoStockArchiveRecordKey,
    BaoStockFieldCoverage,
    BaoStockIncrementCheckpoint,
    BaoStockIncrementCheckpointState,
    BaoStockIncrementManifest,
    BaoStockIncrementPartition,
)
from trader.domain.research.h1_point_in_time import canonical_hash
from trader.infra.research.baostock_catalog import file_sha256


class BaoStockActiveArchiveConflictError(RuntimeError):
    """Raised when an active/increment artifact changes immutable identity."""


@dataclass(frozen=True)
class BaoStockIncrementRecord:
    key: BaoStockArchiveRecordKey
    payload_json: str
    content_hash: str = ""

    def __post_init__(self) -> None:
        try:
            payload = _json_object(json.loads(self.payload_json), "increment record")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("BaoStock increment record payload is invalid") from exc
        canonical = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if (
            canonical != self.payload_json
            or payload.get("code") != self.key.code
            or payload.get("trade_date") != self.key.trade_date.isoformat()
            or not _valid_record_payload(self.key.family, payload)
            or (self.content_hash and self.content_hash != digest)
        ):
            raise ValueError("BaoStock increment record identity is invalid")
        object.__setattr__(self, "content_hash", digest)


def _valid_record_payload(family: BaoStockArchiveFieldFamily, payload: dict[str, object]) -> bool:
    identity = {"code", "trade_date"}
    if family in {"daily_raw", "daily_qfq"}:
        expected = identity | {
            "adjustment",
            "open_price",
            "high_price",
            "low_price",
            "close_price",
            "volume",
            "amount",
            "preclose",
            "pct_change",
            "turnover",
            "trading_status",
        }
        adjustment = "unadjusted" if family == "daily_raw" else "qfq"
        return (
            set(payload) == expected
            and payload.get("adjustment") == adjustment
            and payload.get("trading_status") in {"trading", "suspended"}
            and all(
                value is None
                or isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                for key, value in payload.items()
                if key
                in {
                    "open_price",
                    "high_price",
                    "low_price",
                    "close_price",
                    "volume",
                    "amount",
                    "preclose",
                    "pct_change",
                    "turnover",
                }
            )
            and (
                payload.get("trading_status") == "suspended"
                or all(
                    isinstance(payload.get(key), (int, float))
                    and not isinstance(payload.get(key), bool)
                    and cast(float, payload[key]) > 0
                    for key in ("open_price", "high_price", "low_price", "close_price")
                )
                and all(
                    isinstance(payload.get(key), (int, float))
                    and not isinstance(payload.get(key), bool)
                    and cast(float, payload[key]) >= 0
                    for key in ("volume", "amount")
                )
            )
            and (
                (
                    family == "daily_raw"
                    and (
                        payload.get("trading_status") == "suspended"
                        or isinstance(payload.get("preclose"), (int, float))
                        and not isinstance(payload.get("preclose"), bool)
                        and cast(float, payload["preclose"]) >= 0
                        and isinstance(payload.get("pct_change"), (int, float))
                        and not isinstance(payload.get("pct_change"), bool)
                        and isinstance(payload.get("turnover"), (int, float))
                        and not isinstance(payload.get("turnover"), bool)
                        and cast(float, payload["turnover"]) >= 0
                    )
                )
                or (
                    family == "daily_qfq"
                    and all(payload.get(key) is None for key in ("preclose", "pct_change", "turnover"))
                )
            )
        )
    if family == "is_st":
        return set(payload) == identity | {"is_st"} and isinstance(payload.get("is_st"), bool)
    if family == "industry":
        return (
            set(payload) == identity | {"effective_to", "industry", "classification"}
            and (payload.get("effective_to") is None or isinstance(payload.get("effective_to"), str))
            and all(isinstance(payload.get(key), str) and payload.get(key) for key in ("industry", "classification"))
        )
    timestamp = "published_at" if family == "risk_facts" else "effective_at"
    return timestamp in payload and isinstance(payload[timestamp], str)


class BaoStockIncrementWriter:
    def __init__(self, root: Path, context: BaoStockActiveArchiveContext) -> None:
        self._root = root
        self._context = context
        self._sealed = False

    @property
    def root(self) -> Path:
        return self._root

    @property
    def context(self) -> BaoStockActiveArchiveContext:
        return self._context

    @property
    def record_count(self) -> int:
        return sum(_row_count(path, "records") for path in self._shard_paths())

    def save(self, record: BaoStockIncrementRecord) -> None:
        self._require_open()
        if record.key.trade_date > self._context.source_cutoff:
            raise ValueError("BaoStock increment record exceeds active source cutoff")
        path = self._shard_path(record.key.code)
        _initialize_shard(path, self._context)
        try:
            with sqlite3.connect(path) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT payload_json, content_hash FROM records WHERE code=? AND trade_date=? AND field_family=?",
                    (record.key.code, record.key.trade_date.isoformat(), record.key.family),
                ).fetchone()
                if existing is not None and (existing[0] != record.payload_json or existing[1] != record.content_hash):
                    raise BaoStockActiveArchiveConflictError("BaoStock increment record conflict")
                connection.execute(
                    "INSERT OR IGNORE INTO records(code, trade_date, field_family, payload_json, content_hash) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        record.key.code,
                        record.key.trade_date.isoformat(),
                        record.key.family,
                        record.payload_json,
                        record.content_hash,
                    ),
                )
                connection.execute(
                    "INSERT INTO checkpoints(code, trade_date, field_family, state, error_code, content_hash) "
                    "VALUES (?, ?, ?, 'completed', NULL, ?) "
                    "ON CONFLICT(code, trade_date, field_family) DO UPDATE SET "
                    "state='completed', error_code=NULL, content_hash=excluded.content_hash",
                    (record.key.code, record.key.trade_date.isoformat(), record.key.family, record.content_hash),
                )
        except BaoStockActiveArchiveConflictError:
            raise
        except sqlite3.DatabaseError as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock increment record write failed") from exc

    def record_failure(self, key: BaoStockArchiveRecordKey, error_code: str) -> None:
        self._require_open()
        checkpoint = BaoStockIncrementCheckpoint(key, "failed", error_code=error_code)
        path = self._shard_path(key.code)
        _initialize_shard(path, self._context)
        try:
            with sqlite3.connect(path) as connection:
                completed = connection.execute(
                    "SELECT state FROM checkpoints WHERE code=? AND trade_date=? AND field_family=?",
                    (key.code, key.trade_date.isoformat(), key.family),
                ).fetchone()
                if completed is not None and completed[0] == "completed":
                    return
                connection.execute(
                    "INSERT INTO checkpoints(code, trade_date, field_family, state, error_code, content_hash) "
                    "VALUES (?, ?, ?, 'failed', ?, NULL) "
                    "ON CONFLICT(code, trade_date, field_family) DO UPDATE SET "
                    "state='failed', error_code=excluded.error_code, content_hash=NULL",
                    (key.code, key.trade_date.isoformat(), key.family, checkpoint.error_code),
                )
        except sqlite3.DatabaseError as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock increment failure checkpoint write failed") from exc

    def checkpoint(self, key: BaoStockArchiveRecordKey) -> BaoStockIncrementCheckpoint:
        self._require_open()
        path = self._shard_path(key.code)
        if not path.is_file():
            raise KeyError(key)
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
                row = connection.execute(
                    "SELECT state, error_code, content_hash FROM checkpoints "
                    "WHERE code=? AND trade_date=? AND field_family=?",
                    (key.code, key.trade_date.isoformat(), key.family),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock increment checkpoint is invalid") from exc
        if row is None:
            raise KeyError(key)
        return BaoStockIncrementCheckpoint(
            key,
            cast(BaoStockIncrementCheckpointState, row[0]),
            row[2],
            row[1],
        )

    def completed(self, key: BaoStockArchiveRecordKey) -> bool:
        try:
            return self.checkpoint(key).state == "completed"
        except KeyError:
            return False

    def completed_count(self, family: BaoStockArchiveFieldFamily) -> int:
        return sum(_family_completed_count(path, family) for path in self._shard_paths())

    def _shard_path(self, code: str) -> Path:
        partition = self._context.partition_for(code)
        return self._root / "shards" / f"increment-{partition}.sqlite3"

    def _shard_paths(self) -> tuple[Path, ...]:
        directory = self._root / "shards"
        return tuple(sorted(directory.glob("*.sqlite3"))) if directory.is_dir() else ()

    def _require_open(self) -> None:
        if self._sealed:
            raise RuntimeError("BaoStock increment writer is sealed")

    def _mark_sealed(self) -> None:
        self._sealed = True


class BaoStockActiveArchive:
    def __init__(self, root: Path, context: BaoStockActiveArchiveContext) -> None:
        self._root = root
        self._context = context
        self._active_path = root / "active-manifest.json"
        self._staging = root / ".increment-staging"

    @property
    def context(self) -> BaoStockActiveArchiveContext:
        return self._context

    @classmethod
    def open(cls, root: Path) -> BaoStockActiveArchive:
        active = _decode_active_manifest(root / "active-manifest.json")
        parent_path = root / "manifest.json"
        parent = _read_json(parent_path)
        if (
            parent.get("content_hash") != active.parent_manifest_hash
            or file_sha256(parent_path) != active.parent_manifest_file_hash
        ):
            raise BaoStockActiveArchiveConflictError("BaoStock sealed parent manifest changed")
        context = BaoStockActiveArchiveContext(
            active.parent_manifest_hash,
            active.parent_manifest_file_hash,
            active.source_cutoff,
            active.calendar_hash,
            active.source_identity_hash,
            _partition_map(parent, root),
        )
        archive = cls(root, context)
        archive.verify()
        return archive

    def resume_writer(self) -> BaoStockIncrementWriter:
        self._verify_parent()
        self._root.mkdir(parents=True, exist_ok=True)
        if self._staging.exists():
            stored = _read_context(self._staging / "context.json")
            if stored != self._context:
                if (
                    stored.parent_manifest_hash != self._context.parent_manifest_hash
                    or stored.parent_manifest_file_hash != self._context.parent_manifest_file_hash
                    or stored.partition_by_code != self._context.partition_by_code
                    or stored.source_cutoff > self._context.source_cutoff
                ):
                    raise BaoStockActiveArchiveConflictError("BaoStock increment staging context conflict")
                for path in sorted((self._staging / "shards").glob("*.sqlite3")):
                    _rebind_staging_shard(path, self._context)
                _write_json(self._staging / "context.json", _encode_context(self._context))
            return BaoStockIncrementWriter(self._staging, self._context)
        temporary = Path(tempfile.mkdtemp(prefix=".increment-staging.", dir=self._root))
        try:
            active = self._verify_active(allow_missing=True, require_current_context=False)
            if active is not None:
                source = self._root / active.increment_manifest_path
                for reference in _read_increment_manifest(source).partitions:
                    target = temporary / reference.relative_path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source.parent / reference.relative_path, target)
                    _rebind_staging_shard(target, self._context)
            _write_json(temporary / "context.json", _encode_context(self._context))
            os.replace(temporary, self._staging)
            _fsync_directory(self._root)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        return BaoStockIncrementWriter(self._staging, self._context)

    def publish(
        self,
        writer: BaoStockIncrementWriter,
        field_coverage: tuple[BaoStockFieldCoverage, ...],
    ) -> BaoStockActiveManifest:
        self._verify_parent()
        if writer.root != self._staging or writer.context != self._context:
            raise ValueError("BaoStock increment writer does not belong to this active archive")
        coverage = tuple(field_coverage)
        if tuple(item.family for item in sorted(coverage, key=_coverage_order)) != BAOSTOCK_ARCHIVE_FIELD_FAMILIES:
            raise ValueError("BaoStock active field coverage is incomplete")
        _validate_staging_against_parent(self._root, self._staging)
        partitions, records_hash, checkpoints_hash = _seal_staging(self._staging, self._context)
        increment = BaoStockIncrementManifest(
            self._context.parent_manifest_hash,
            self._context.parent_manifest_file_hash,
            self._context.source_cutoff,
            self._context.calendar_hash,
            self._context.source_identity_hash,
            partitions,
            records_hash,
            checkpoints_hash,
        )
        _write_json(self._staging / "manifest.json", _encode_increment_manifest(increment))
        destination = self._root / "increments" / increment.content_hash
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            existing = _read_increment_manifest(destination / "manifest.json")
            if existing != increment:
                raise BaoStockActiveArchiveConflictError("BaoStock increment generation conflict")
            shutil.rmtree(self._staging)
        else:
            os.replace(self._staging, destination)
            _fsync_directory(destination.parent)
        active = BaoStockActiveManifest(
            self._context.parent_manifest_hash,
            self._context.parent_manifest_file_hash,
            increment.content_hash,
            f"increments/{increment.content_hash}/manifest.json",
            self._context.source_cutoff,
            self._context.calendar_hash,
            self._context.source_identity_hash,
            coverage,
        )
        self._verify_increment(active)
        _write_json(self._active_path, _encode_active_manifest(active))
        writer._mark_sealed()
        return active

    def verify(self) -> BaoStockActiveManifest:
        self._verify_parent()
        active = self._verify_active(allow_missing=False, require_current_context=True)
        if active is None:  # pragma: no cover - allow_missing=False rejects this path
            raise BaoStockActiveArchiveConflictError("BaoStock active manifest is unavailable")
        return active

    def _verify_parent(self) -> None:
        path = self._root / "manifest.json"
        try:
            raw = _read_json(path)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock sealed parent manifest changed") from exc
        if (
            file_sha256(path) != self._context.parent_manifest_file_hash
            or raw.get("content_hash") != self._context.parent_manifest_hash
        ):
            raise BaoStockActiveArchiveConflictError("BaoStock sealed parent manifest changed")

    def read_increment_record(self, key: BaoStockArchiveRecordKey) -> BaoStockIncrementRecord | None:
        active = self.verify()
        return self._read_verified_increment_record(active, key)

    def _read_verified_increment_record(
        self,
        active: BaoStockActiveManifest,
        key: BaoStockArchiveRecordKey,
    ) -> BaoStockIncrementRecord | None:
        manifest_path = self._root / active.increment_manifest_path
        partition = self._context.partition_for(key.code)
        path = manifest_path.parent / "shards" / f"increment-{partition}.sqlite3"
        if not path.is_file():
            return None
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
                row = connection.execute(
                    "SELECT payload_json, content_hash FROM records WHERE code=? AND trade_date=? AND field_family=?",
                    (key.code, key.trade_date.isoformat(), key.family),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock active increment record is invalid") from exc
        if row is None:
            return None
        try:
            return BaoStockIncrementRecord(key, cast(str, row[0]), cast(str, row[1]))
        except ValueError as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock active increment record is invalid") from exc

    def _verify_active(
        self,
        *,
        allow_missing: bool,
        require_current_context: bool,
    ) -> BaoStockActiveManifest | None:
        if not self._active_path.is_file():
            if allow_missing:
                return None
            raise BaoStockActiveArchiveConflictError("BaoStock active manifest is unavailable")
        try:
            active = _decode_active_manifest(self._active_path)
            if (
                active.parent_manifest_hash != self._context.parent_manifest_hash
                or active.parent_manifest_file_hash != self._context.parent_manifest_file_hash
                or active.source_cutoff > self._context.source_cutoff
                or (
                    require_current_context
                    and (
                        active.source_cutoff != self._context.source_cutoff
                        or active.calendar_hash != self._context.calendar_hash
                        or active.source_identity_hash != self._context.source_identity_hash
                    )
                )
            ):
                raise ValueError("active context mismatch")
            self._verify_increment(active)
            return active
        except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock active manifest is invalid") from exc

    def _verify_increment(self, active: BaoStockActiveManifest) -> BaoStockIncrementManifest:
        manifest_path = (self._root / active.increment_manifest_path).resolve()
        if self._root.resolve() not in manifest_path.parents:
            raise BaoStockActiveArchiveConflictError("BaoStock increment manifest path is invalid")
        try:
            increment = _read_increment_manifest(manifest_path)
            if (
                increment.content_hash != active.increment_manifest_hash
                or increment.parent_manifest_hash != active.parent_manifest_hash
                or increment.parent_manifest_file_hash != active.parent_manifest_file_hash
                or increment.source_cutoff != active.source_cutoff
                or increment.calendar_hash != active.calendar_hash
                or increment.source_identity_hash != active.source_identity_hash
            ):
                raise ValueError("increment identity mismatch")
            for reference in increment.partitions:
                path = manifest_path.parent / reference.relative_path
                if file_sha256(path) != reference.sha256 or _row_count(path, "records") != reference.record_count:
                    raise ValueError("increment partition mismatch")
                if _row_count(path, "checkpoints") != reference.checkpoint_count:
                    raise ValueError("increment checkpoint mismatch")
            return increment
        except (OSError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock increment manifest is invalid") from exc


class BaoStockActiveArchiveView:
    """Typed read-through view over a sealed parent and one verified increment."""

    def __init__(self, root: Path, increment: BaoStockActiveArchive) -> None:
        self._root = root
        self._increment = increment
        self._active = increment.verify()

    def read(self, key: BaoStockArchiveRecordKey) -> BaoStockIncrementRecord | None:
        child = self._increment._read_verified_increment_record(self._active, key)
        parent = self._read_parent(key)
        if child is not None and parent is not None and child.content_hash != parent.content_hash:
            raise BaoStockActiveArchiveConflictError("BaoStock parent/increment record conflict")
        return child or parent

    def _read_parent(self, key: BaoStockArchiveRecordKey) -> BaoStockIncrementRecord | None:
        partition = self._increment.context.partition_for(key.code)
        path = self._root / "shards" / f"{partition}.sqlite3"
        if not path.is_file() or key.family in {"qualification", "hard_filter", "risk_facts"}:
            return None
        try:
            with sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
                payload = _parent_payload(connection, key)
        except (json.JSONDecodeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock sealed parent record is invalid") from exc
        if payload is None:
            return None
        encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return BaoStockIncrementRecord(key, encoded)


def _parent_payload(connection: sqlite3.Connection, key: BaoStockArchiveRecordKey) -> dict[str, object] | None:
    identity = (key.code, key.trade_date.isoformat())
    if key.family in {"daily_raw", "daily_qfq"}:
        row = connection.execute(
            "SELECT payload_json FROM daily_cells WHERE code=? AND trade_date=?",
            identity,
        ).fetchone()
        if row is None:
            return None
        cell = _json_object(json.loads(cast(str, row[0])), "parent daily cell")
        side_name = "unadjusted" if key.family == "daily_raw" else "qfq"
        side = cell.get(side_name)
        return None if side is None else _json_object(side, "parent daily side")
    if key.family == "is_st":
        row = connection.execute(
            "SELECT is_st FROM daily_facts WHERE code=? AND trade_date=?",
            identity,
        ).fetchone()
        if row is None or row[0] not in (0, 1):
            return None
        return {"code": key.code, "trade_date": key.trade_date.isoformat(), "is_st": bool(row[0])}
    row = connection.execute(
        "SELECT effective_to, industry, classification FROM industry_intervals WHERE code=? AND effective_from=?",
        identity,
    ).fetchone()
    if row is None:
        return None
    return {
        "code": key.code,
        "trade_date": key.trade_date.isoformat(),
        "effective_to": row[0],
        "industry": row[1],
        "classification": row[2],
    }


def _validate_staging_against_parent(root: Path, staging: Path) -> None:
    for increment_path in sorted((staging / "shards").glob("increment-*.sqlite3")):
        partition = increment_path.stem.removeprefix("increment-")
        parent_path = root / "shards" / f"{partition}.sqlite3"
        if not parent_path.is_file():
            raise BaoStockActiveArchiveConflictError("BaoStock sealed parent partition is missing")
        try:
            with (
                sqlite3.connect(f"file:{increment_path.as_posix()}?mode=ro", uri=True) as increment,
                sqlite3.connect(f"file:{parent_path.as_posix()}?mode=ro&immutable=1", uri=True) as parent,
            ):
                rows = increment.execute(
                    "SELECT code, trade_date, field_family, payload_json, content_hash FROM records "
                    "ORDER BY code, trade_date, field_family"
                )
                for code, day, family, payload_json, content_hash in rows:
                    key = BaoStockArchiveRecordKey(
                        cast(str, code),
                        date.fromisoformat(cast(str, day)),
                        cast(BaoStockArchiveFieldFamily, family),
                    )
                    record = BaoStockIncrementRecord(key, cast(str, payload_json), cast(str, content_hash))
                    parent_payload = _parent_payload(parent, key)
                    if parent_payload is None:
                        continue
                    encoded = json.dumps(
                        parent_payload,
                        ensure_ascii=True,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    parent_record = BaoStockIncrementRecord(key, encoded)
                    if parent_record.content_hash != record.content_hash:
                        raise BaoStockActiveArchiveConflictError("BaoStock parent/increment record conflict")
        except BaoStockActiveArchiveConflictError:
            raise
        except (json.JSONDecodeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
            raise BaoStockActiveArchiveConflictError("BaoStock parent/increment validation failed") from exc


def _initialize_shard(path: Path, context: BaoStockActiveArchiveContext) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS archive_context (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    context_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS records (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    field_family TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY(code, trade_date, field_family)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    code TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    field_family TEXT NOT NULL,
                    state TEXT NOT NULL,
                    error_code TEXT,
                    content_hash TEXT,
                    PRIMARY KEY(code, trade_date, field_family)
                );
                """
            )
            row = connection.execute("SELECT context_hash FROM archive_context WHERE singleton=1").fetchone()
            if row is None:
                connection.execute("INSERT INTO archive_context VALUES (1, ?)", (context.content_hash,))
            elif row[0] != context.content_hash:
                raise BaoStockActiveArchiveConflictError("BaoStock increment shard context conflict")
    except BaoStockActiveArchiveConflictError:
        raise
    except sqlite3.DatabaseError as exc:
        raise BaoStockActiveArchiveConflictError("BaoStock increment shard is invalid") from exc


def _rebind_staging_shard(path: Path, context: BaoStockActiveArchiveContext) -> None:
    """Bind a verified generation copy to the next active context."""
    try:
        with sqlite3.connect(path) as connection:
            row = connection.execute("SELECT context_hash FROM archive_context WHERE singleton=1").fetchone()
            if row is None:
                raise BaoStockActiveArchiveConflictError("BaoStock increment shard context is missing")
            if row[0] == context.content_hash:
                return
            connection.execute(
                "UPDATE archive_context SET context_hash=? WHERE singleton=1",
                (context.content_hash,),
            )
    except BaoStockActiveArchiveConflictError:
        raise
    except sqlite3.DatabaseError as exc:
        raise BaoStockActiveArchiveConflictError("BaoStock increment shard is invalid") from exc


def _seal_staging(
    root: Path,
    context: BaoStockActiveArchiveContext,
) -> tuple[tuple[BaoStockIncrementPartition, ...], str, str]:
    paths = tuple(sorted((root / "shards").glob("*.sqlite3"))) if (root / "shards").is_dir() else ()
    record_identities: list[tuple[str, str, str, str]] = []
    checkpoint_identities: list[tuple[str, str, str, str, str | None, str | None]] = []
    references: list[BaoStockIncrementPartition] = []
    for path in paths:
        _initialize_shard(path, context)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity != ("ok",):
                raise BaoStockActiveArchiveConflictError("BaoStock increment database integrity is invalid")
            records = connection.execute(
                "SELECT code, trade_date, field_family, content_hash, payload_json FROM records "
                "ORDER BY code, trade_date, field_family"
            ).fetchall()
            checkpoints = connection.execute(
                "SELECT code, trade_date, field_family, state, error_code, content_hash FROM checkpoints "
                "ORDER BY code, trade_date, field_family"
            ).fetchall()
        for code, day, family, content_hash, payload_json in records:
            record = BaoStockIncrementRecord(
                BaoStockArchiveRecordKey(
                    cast(str, code), date.fromisoformat(cast(str, day)), cast(BaoStockArchiveFieldFamily, family)
                ),
                cast(str, payload_json),
                cast(str, content_hash),
            )
            record_identities.append(
                (record.key.code, record.key.trade_date.isoformat(), record.key.family, record.content_hash)
            )
        for code, day, family, state, error_code, content_hash in checkpoints:
            checkpoint = BaoStockIncrementCheckpoint(
                BaoStockArchiveRecordKey(
                    cast(str, code), date.fromisoformat(cast(str, day)), cast(BaoStockArchiveFieldFamily, family)
                ),
                cast(BaoStockIncrementCheckpointState, state),
                cast(str | None, content_hash),
                cast(str | None, error_code),
            )
            checkpoint_identities.append(
                (
                    checkpoint.key.code,
                    checkpoint.key.trade_date.isoformat(),
                    checkpoint.key.family,
                    checkpoint.state,
                    checkpoint.error_code,
                    checkpoint.content_hash,
                )
            )
        references.append(
            BaoStockIncrementPartition(
                f"shards/{path.name}",
                file_sha256(path),
                len(records),
                len(checkpoints),
            )
        )
    return tuple(references), canonical_hash(tuple(record_identities)), canonical_hash(tuple(checkpoint_identities))


def _row_count(path: Path, table: str) -> int:
    if table not in {"records", "checkpoints"}:
        raise ValueError("BaoStock increment table is invalid")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    return int(row[0]) if row is not None else 0


def _family_completed_count(path: Path, family: BaoStockArchiveFieldFamily) -> int:
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE field_family=? AND state='completed'",
            (family,),
        ).fetchone()
    return int(row[0]) if row is not None else 0


def _coverage_order(value: BaoStockFieldCoverage) -> int:
    return BAOSTOCK_ARCHIVE_FIELD_FAMILIES.index(value.family)


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _encode_context(value: BaoStockActiveArchiveContext) -> dict[str, object]:
    return {
        "schema_version": "baostock_active_archive_context",
        "parent_manifest_hash": value.parent_manifest_hash,
        "parent_manifest_file_hash": value.parent_manifest_file_hash,
        "source_cutoff": value.source_cutoff.isoformat(),
        "calendar_hash": value.calendar_hash,
        "source_identity_hash": value.source_identity_hash,
        "partition_by_code": [list(item) for item in value.partition_by_code],
        "content_hash": value.content_hash,
    }


def _read_context(path: Path) -> BaoStockActiveArchiveContext:
    raw = _read_json(path)
    expected = {
        "schema_version",
        "parent_manifest_hash",
        "parent_manifest_file_hash",
        "source_cutoff",
        "calendar_hash",
        "source_identity_hash",
        "partition_by_code",
        "content_hash",
    }
    if set(raw) != expected or raw["schema_version"] != "baostock_active_archive_context":
        raise BaoStockActiveArchiveConflictError("BaoStock increment staging context is invalid")
    pairs = _pairs(raw["partition_by_code"])
    value = BaoStockActiveArchiveContext(
        _string(raw["parent_manifest_hash"]),
        _string(raw["parent_manifest_file_hash"]),
        date.fromisoformat(_string(raw["source_cutoff"])),
        _string(raw["calendar_hash"]),
        _string(raw["source_identity_hash"]),
        pairs,
    )
    if value.content_hash != raw["content_hash"]:
        raise BaoStockActiveArchiveConflictError("BaoStock increment staging context hash is invalid")
    return value


def _encode_increment_manifest(value: BaoStockIncrementManifest) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "parent_manifest_hash": value.parent_manifest_hash,
        "parent_manifest_file_hash": value.parent_manifest_file_hash,
        "source_cutoff": value.source_cutoff.isoformat(),
        "calendar_hash": value.calendar_hash,
        "source_identity_hash": value.source_identity_hash,
        "partitions": [
            {
                "schema_version": item.schema_version,
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "record_count": item.record_count,
                "checkpoint_count": item.checkpoint_count,
            }
            for item in value.partitions
        ],
        "records_hash": value.records_hash,
        "checkpoints_hash": value.checkpoints_hash,
        "production_authority": False,
        "point_in_time_parity": False,
        "content_hash": value.content_hash,
    }


def _read_increment_manifest(path: Path) -> BaoStockIncrementManifest:
    raw = _read_json(path)
    expected = {
        "schema_version",
        "parent_manifest_hash",
        "parent_manifest_file_hash",
        "source_cutoff",
        "calendar_hash",
        "source_identity_hash",
        "partitions",
        "records_hash",
        "checkpoints_hash",
        "production_authority",
        "point_in_time_parity",
        "content_hash",
    }
    if set(raw) != expected:
        raise ValueError("increment manifest fields are invalid")
    partitions_raw = _list(raw.get("partitions"), "increment partitions")
    partitions = tuple(
        BaoStockIncrementPartition(
            _string(item["relative_path"]),
            _string(item["sha256"]),
            _integer(item["record_count"]),
            _integer(item["checkpoint_count"]),
            _string(item["schema_version"]),
        )
        for item in (_increment_partition(value) for value in partitions_raw)
    )
    manifest = BaoStockIncrementManifest(
        _string(raw["parent_manifest_hash"]),
        _string(raw["parent_manifest_file_hash"]),
        date.fromisoformat(_string(raw["source_cutoff"])),
        _string(raw["calendar_hash"]),
        _string(raw["source_identity_hash"]),
        partitions,
        _string(raw["records_hash"]),
        _string(raw["checkpoints_hash"]),
        _boolean(raw["production_authority"]),
        _boolean(raw["point_in_time_parity"]),
        _string(raw["schema_version"]),
    )
    if manifest.content_hash != raw.get("content_hash"):
        raise ValueError("increment manifest content hash mismatch")
    return manifest


def _encode_active_manifest(value: BaoStockActiveManifest) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "parent_manifest_hash": value.parent_manifest_hash,
        "parent_manifest_file_hash": value.parent_manifest_file_hash,
        "increment_manifest_hash": value.increment_manifest_hash,
        "increment_manifest_path": value.increment_manifest_path,
        "active_data_hash": value.active_data_hash,
        "source_cutoff": value.source_cutoff.isoformat(),
        "calendar_hash": value.calendar_hash,
        "source_identity_hash": value.source_identity_hash,
        "field_coverage": [
            {
                "family": item.family,
                "reusable_rows": item.reusable_rows,
                "incremental_rows": item.incremental_rows,
                "missing_rows": item.missing_rows,
                "missing_reason": item.missing_reason,
            }
            for item in value.field_coverage
        ],
        "production_authority": False,
        "point_in_time_parity": False,
        "content_hash": value.content_hash,
    }


def _decode_active_manifest(path: Path) -> BaoStockActiveManifest:
    raw = _read_json(path)
    expected = {
        "schema_version",
        "parent_manifest_hash",
        "parent_manifest_file_hash",
        "increment_manifest_hash",
        "increment_manifest_path",
        "active_data_hash",
        "source_cutoff",
        "calendar_hash",
        "source_identity_hash",
        "field_coverage",
        "production_authority",
        "point_in_time_parity",
        "content_hash",
    }
    if set(raw) != expected:
        raise ValueError("active manifest fields are invalid")
    coverage = tuple(
        BaoStockFieldCoverage(
            cast(BaoStockArchiveFieldFamily, item["family"]),
            _integer(item["reusable_rows"]),
            _integer(item["incremental_rows"]),
            _integer(item["missing_rows"]),
            _optional_string(item["missing_reason"]),
        )
        for item in (_json_object(value, "field coverage") for value in _list(raw["field_coverage"], "field coverage"))
    )
    active = BaoStockActiveManifest(
        _string(raw["parent_manifest_hash"]),
        _string(raw["parent_manifest_file_hash"]),
        _string(raw["increment_manifest_hash"]),
        _string(raw["increment_manifest_path"]),
        date.fromisoformat(_string(raw["source_cutoff"])),
        _string(raw["calendar_hash"]),
        _string(raw["source_identity_hash"]),
        coverage,
        _boolean(raw["production_authority"]),
        _boolean(raw["point_in_time_parity"]),
        _string(raw["schema_version"]),
    )
    if active.active_data_hash != raw.get("active_data_hash") or active.content_hash != raw.get("content_hash"):
        raise ValueError("active manifest content hash mismatch")
    return active


def _increment_partition(value: object) -> dict[str, object]:
    item = _json_object(value, "increment partition")
    if set(item) != {"schema_version", "relative_path", "sha256", "record_count", "checkpoint_count"}:
        raise ValueError("increment partition fields are invalid")
    return item


def _partition_map(parent: dict[str, object], root: Path) -> tuple[tuple[str, str], ...]:
    values = _list(parent.get("partitions"), "parent partitions")
    pairs: list[tuple[str, str]] = []
    for value in values:
        item = _json_object(value, "parent partition")
        relative = Path(_string(item.get("relative_path")))
        path = (root / relative).resolve()
        if root.resolve() not in path.parents or not path.is_file() or relative.suffix != ".sqlite3":
            raise BaoStockActiveArchiveConflictError("BaoStock parent partition path is invalid")
        codes = _list(item.get("codes"), "parent partition codes")
        pairs.extend((_string(code), relative.stem) for code in codes)
    return tuple(pairs)


def _read_json(path: Path) -> dict[str, object]:
    return _json_object(json.loads(path.read_text(encoding="utf-8")), path.name)


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"BaoStock {label} must be an object")
    return cast(dict[str, object], value)


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"BaoStock {label} must be a list")
    return value


def _pairs(value: object) -> tuple[tuple[str, str], ...]:
    rows = _list(value, "partition map")
    pairs: list[tuple[str, str]] = []
    for row in rows:
        values = _list(row, "partition map row")
        if len(values) != 2:
            raise TypeError("BaoStock partition map row is invalid")
        pairs.append((_string(values[0]), _string(values[1])))
    return tuple(pairs)


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypeError("BaoStock manifest string is invalid")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("BaoStock manifest integer is invalid")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise TypeError("BaoStock manifest boolean is invalid")
    return value


__all__ = [
    "BaoStockActiveArchive",
    "BaoStockActiveArchiveView",
    "BaoStockActiveArchiveConflictError",
    "BaoStockIncrementRecord",
    "BaoStockIncrementWriter",
]
