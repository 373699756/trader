#!/usr/bin/env python3
"""Convert the sealed BaoStock parent/increment archive into monthly SQLite shards.

The converter is deliberately an offline, single-process tool. It reads the
legacy archive without writing it, builds one month at a time in a resumable
sibling staging directory, and publishes the target directory with one rename.
"""

from __future__ import annotations

import argparse
import bisect
import errno
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, closing
from dataclasses import dataclass, replace
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from trader.domain.research.baostock_daily import BaoStockDailyCell, BaoStockDailySide
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryCalendarIdentity,
    HistorySecurityBoard,
    HistorySecurityIdentity,
    HistorySnapshotPartition,
    HistorySourceIdentity,
    HistorySyncCheckpoint,
    HistoryTrainingDueState,
    HistoryUniverseIdentity,
)
from trader.domain.research.history_monthly import HistoryMonthlyRevision
from trader.infra.research.baostock_gap_supplier import (
    BaoStockGapFamily,
    BaoStockGapRecord,
    BaoStockGapRequest,
    BaoStockGapResult,
    BaoStockGapSupplierError,
    fetch_baostock_gaps,
)
from trader.infra.research.history_archive_sync import (
    _backup_database,
    _discard_partition_replacements,
    _PendingPartitions,
    _publish_snapshot,
    _recover_partition_replacements,
    _restore_partition_replacements,
    _seal_pending,
    _write_revisions,
)
from trader.infra.research.history_archive_sync import (
    _remove_pending as _remove_sync_pending,
)
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    HistoryMaintenanceAlreadyRunningError,
    HistoryMaintenanceLock,
    SQLiteHistoryControlRepository,
)
from trader.infra.research.history_month_codec import (
    decode_history_monthly_revision,
    encode_history_monthly_revision,
)
from trader.infra.research.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)

DEFAULT_SOURCE = Path("data/history/baostock-daily/sessions-2000")
DEFAULT_TARGET = Path("data/history/baostock")
DEFAULT_BATCH_SIZE = 256
DEFAULT_CACHE_MIB = 8
DEFAULT_THROTTLE_MS = 5
DEFAULT_MINIMUM_FREE_MIB = 2048
DEFAULT_NICE_INCREMENT = 10
_HASH_CHUNK_BYTES = 4 * 1024 * 1024
_PROGRESS_SCHEMA = "baostock_history_conversion_progress"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SOURCE_FAMILIES = frozenset(
    {"daily_raw", "daily_qfq", "is_st", "industry", "qualification", "hard_filter", "risk_facts"}
)
DOWNLOADABLE_FIELD_FAMILIES = frozenset({"daily_raw", "daily_qfq", "is_st"})


class ConversionError(RuntimeError):
    """Raised when conversion cannot safely produce an atomic target."""


@dataclass(frozen=True)
class SourcePartition:
    relative_path: str
    path: Path
    expected_sha256: str
    expected_rows: int
    board: str


@dataclass(frozen=True)
class IncrementPartition:
    relative_path: str
    path: Path
    expected_sha256: str
    expected_rows: int


@dataclass(frozen=True)
class Security:
    code: str
    name: str
    board: str
    listed_on: str
    delisted_on: str | None
    source_version: str


@dataclass(frozen=True)
class IndustryInterval:
    effective_from: str
    effective_to: str | None
    industry: str
    classification: str
    content_hash: str


@dataclass(frozen=True)
class SourceArchive:
    root: Path
    source_fingerprint: str
    manifest_file_hash: str
    active_manifest_file_hash: str
    parent_manifest_hash: str
    active_data_hash: str
    source_identity_hash: str
    source_cutoff: str
    sessions: int
    calendar_dates: tuple[str, ...]
    securities: tuple[Security, ...]
    source_versions_json: str
    parent_partitions: tuple[SourcePartition, ...]
    increment_partitions: tuple[IncrementPartition, ...]
    field_coverage: tuple[tuple[str, int, int, int, str | None], ...]
    production_authority: bool
    point_in_time_parity: bool


@dataclass(frozen=True)
class DailyRecord:
    trade_date: str
    code: str
    sync_sequence: int
    board: str
    status: str
    raw_payload_json: str | None
    qfq_payload_json: str | None
    is_st: int | None
    industry: str | None
    industry_classification: str | None
    raw_content_hash: str | None
    qfq_content_hash: str | None
    is_st_content_hash: str | None
    industry_content_hash: str | None
    row_hash: str

    @property
    def revision_id(self) -> str:
        return self.row_hash


@dataclass(frozen=True)
class PartitionResult:
    year: int
    month: int
    relative_path: str
    database_sha256: str
    logical_content_hash: str
    physical_rows: int
    active_rows: int
    source_rows: int


@dataclass(frozen=True)
class ConversionSummary:
    state: str
    partition_count: int
    physical_rows: int
    active_rows: int
    active_calendar_days: int
    data_cutoff: str
    snapshot_hash: str
    batch_size: int
    cache_mib: int
    throttle_ms: int
    supplemented_rows: int
    remaining_downloadable_gaps: int


ProgressSink = Callable[["ConversionProgress"], None]
SupplementProvider = Callable[..., BaoStockGapResult]
FaultInjector = Callable[[str], None]


@dataclass(frozen=True)
class _HashLayoutPartition:
    previous_relative_path: str
    current_reference: HistorySnapshotPartition


@dataclass(frozen=True)
class _HashLayoutSnapshot:
    previous_hash: str
    current: HistoryActiveSnapshot
    partitions: tuple[_HashLayoutPartition, ...]


@dataclass(frozen=True)
class ConversionProgress:
    phase: str
    completed: int
    total: int
    current: str
    elapsed_seconds: float

    @property
    def percentage(self) -> float:
        return 100.0 if self.total == 0 else min(100.0, self.completed * 100.0 / self.total)


class _ProgressTracker:
    def __init__(self, phase: str, total: int, sink: ProgressSink | None) -> None:
        self._phase = phase
        self._total = total
        self._sink = sink
        self._completed = 0
        self._started = time.monotonic()
        self._last_emitted = 0.0
        self.emit("-", force=True)

    def advance(self, amount: int, current: str, *, force: bool = False) -> None:
        self._completed += amount
        if self._completed > self._total:
            raise ConversionError("conversion progress exceeded its declared source rows")
        self.emit(current, force=force or self._completed == self._total)

    def emit(self, current: str, *, force: bool) -> None:
        now = time.monotonic()
        if self._sink is None or (not force and now - self._last_emitted < 1.0):
            return
        self._sink(ConversionProgress(self._phase, self._completed, self._total, current, now - self._started))
        self._last_emitted = now


class _SourceLock(AbstractContextManager["_SourceLock"]):
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def __enter__(self) -> _SourceLock:
        if not self._path.is_file():
            raise ConversionError("source download lock is missing")
        self._handle = self._path.open("a+")
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError as exc:  # pragma: no cover - exercised on non-POSIX hosts
            self._handle.close()
            self._handle = None
            raise ConversionError("source lock is unsupported on this platform") from exc
        except OSError as exc:
            self._handle.close()
            self._handle = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise ConversionError("source download is already running") from exc
            raise ConversionError("source lock could not be acquired") from exc
        return self

    def __exit__(self, *_args: object) -> None:
        if self._handle is None:
            return
        try:
            import fcntl

            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class _Cancellation:
    def __init__(self) -> None:
        self.requested = False
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> _Cancellation:
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._request)
        return self

    def __exit__(self, *_args: object) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)

    def _request(self, _signum: int, _frame: object) -> None:
        self.requested = True

    def check(self) -> None:
        if self.requested:
            raise ConversionError("conversion cancelled; completed months remain resumable")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path: Path, throttle_seconds: float = 0.0, cancellation: _Cancellation | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            if cancellation is not None:
                cancellation.check()
            if throttle_seconds > 0:
                time.sleep(throttle_seconds)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionError(f"{label} is unreadable") from exc
    if not isinstance(value, dict):
        raise ConversionError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConversionError(f"{label} must be a non-empty string")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConversionError(f"{label} must be a non-negative integer")
    return value


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ConversionError(f"{label} must be boolean")
    return value


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise ConversionError(f"{label} must be an array")
    return cast(list[object], value)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConversionError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _safe_child(root: Path, relative: str, label: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ConversionError(f"{label} path is unsafe")
    resolved = (root / candidate).resolve()
    if root.resolve() not in resolved.parents:
        raise ConversionError(f"{label} path escapes its archive")
    return resolved


def _parse_security(raw: object) -> Security:
    value = _object(raw, "security")
    delisted = value.get("delisted_on")
    if delisted is not None and not isinstance(delisted, str):
        raise ConversionError("security delisted_on must be a date or null")
    return Security(
        code=_string(value.get("code"), "security code"),
        name=_string(value.get("name"), "security name"),
        board=_string(value.get("board"), "security board"),
        listed_on=_string(value.get("listed_on"), "security listed_on"),
        delisted_on=delisted,
        source_version=_string(value.get("source_version"), "security source_version"),
    )


def _load_context(path: Path) -> tuple[dict[str, object], tuple[str, ...], tuple[Security, ...], str]:
    try:
        with closing(sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)) as connection:
            row = connection.execute(
                "SELECT spec_json, calendar_json, universe_json, versions_json FROM context WHERE singleton=1"
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise ConversionError("parent archive context is unreadable") from exc
    if row is None:
        raise ConversionError("parent archive context is missing")
    try:
        spec = _object(json.loads(row[0]), "parent spec")
        calendar = _object(json.loads(row[1]), "parent calendar")
        securities = tuple(_parse_security(item) for item in _list(json.loads(row[2]), "parent universe"))
        versions = _canonical_json(_object(json.loads(row[3]), "source versions"))
        dates = tuple(_string(item, "calendar date") for item in _list(calendar.get("open_dates"), "calendar dates"))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConversionError("parent archive context JSON is invalid") from exc
    if not dates or dates != tuple(sorted(set(dates))):
        raise ConversionError("parent calendar is empty, duplicated, or unordered")
    if not securities or len({item.code for item in securities}) != len(securities):
        raise ConversionError("parent security universe is empty or duplicated")
    return spec, dates, securities, versions


def _parse_parent_partitions(root: Path, manifest: dict[str, object]) -> tuple[SourcePartition, ...]:
    parsed: list[SourcePartition] = []
    for item in _list(manifest.get("partitions"), "parent partitions"):
        value = _object(item, "parent partition")
        relative = _string(value.get("relative_path"), "parent partition relative_path")
        parsed.append(
            SourcePartition(
                relative_path=relative,
                path=_safe_child(root, relative, "parent partition"),
                expected_sha256=_string(value.get("database_sha256"), "parent partition SHA-256"),
                expected_rows=_integer(value.get("row_count"), "parent partition row count"),
                board=_string(value.get("board"), "parent partition board"),
            )
        )
    if not parsed:
        raise ConversionError("parent manifest contains no partitions")
    return tuple(sorted(parsed, key=lambda item: item.relative_path))


def _parse_increment_partitions(
    root: Path,
    active: dict[str, object],
    manifest: dict[str, object],
) -> tuple[IncrementPartition, ...]:
    relative_manifest = _string(active.get("increment_manifest_path"), "increment manifest path")
    manifest_path = _safe_child(root, relative_manifest, "increment manifest")
    manifest_root = manifest_path.parent
    parsed: list[IncrementPartition] = []
    for item in _list(manifest.get("partitions"), "increment partitions"):
        value = _object(item, "increment partition")
        relative = _string(value.get("relative_path"), "increment partition relative_path")
        parsed.append(
            IncrementPartition(
                relative_path=str(Path(relative_manifest).parent / relative),
                path=_safe_child(manifest_root, relative, "increment partition"),
                expected_sha256=_string(value.get("sha256"), "increment partition SHA-256"),
                expected_rows=_integer(value.get("record_count"), "increment partition row count"),
            )
        )
    return tuple(sorted(parsed, key=lambda item: item.relative_path))


def _field_coverage(active: dict[str, object]) -> tuple[tuple[str, int, int, int, str | None], ...]:
    rows: list[tuple[str, int, int, int, str | None]] = []
    for item in _list(active.get("field_coverage", []), "field coverage"):
        value = _object(item, "field coverage row")
        family = _string(value.get("family"), "field coverage family")
        if family not in _SOURCE_FAMILIES:
            raise ConversionError("field coverage contains an unsupported family")
        reason = value.get("missing_reason")
        if reason is not None and not isinstance(reason, str):
            raise ConversionError("field coverage missing reason must be a string or null")
        rows.append(
            (
                family,
                _integer(value.get("reusable_rows"), "reusable rows"),
                _integer(value.get("incremental_rows"), "incremental rows"),
                _integer(value.get("missing_rows"), "missing rows"),
                reason,
            )
        )
    return tuple(sorted(rows))


def _load_source(root: Path) -> SourceArchive:
    manifest_path = root / "manifest.json"
    active_path = root / "active-manifest.json"
    manifest = _load_json(manifest_path, "parent manifest")
    active = _load_json(active_path, "active manifest")
    if manifest.get("schema_version") != "baostock_daily_manifest":
        raise ConversionError("parent manifest schema is unsupported")
    if active.get("schema_version") != "baostock_active_manifest":
        raise ConversionError("active manifest schema is unsupported")
    manifest_hash = _hash_file(manifest_path)
    increment_manifest_path = _safe_child(
        root,
        _string(active.get("increment_manifest_path"), "increment manifest path"),
        "increment manifest",
    )
    increment_manifest = _load_json(increment_manifest_path, "increment manifest")
    identity_fields = (
        "parent_manifest_hash",
        "parent_manifest_file_hash",
        "source_cutoff",
        "calendar_hash",
        "source_identity_hash",
    )
    if (
        active.get("parent_manifest_hash") != manifest.get("content_hash")
        or active.get("parent_manifest_file_hash") != manifest_hash
        or active.get("increment_manifest_hash") != increment_manifest.get("content_hash")
        or any(increment_manifest.get(field) != active.get(field) for field in identity_fields)
        or _boolean(active.get("production_authority"), "production authority")
        or _boolean(active.get("point_in_time_parity"), "point-in-time parity")
        or _boolean(increment_manifest.get("production_authority"), "increment production authority")
        or _boolean(increment_manifest.get("point_in_time_parity"), "increment point-in-time parity")
    ):
        raise ConversionError("source manifest identity chain is inconsistent")
    parent_partitions = _parse_parent_partitions(root, manifest)
    increment_partitions = _parse_increment_partitions(root, active, increment_manifest)
    spec, dates, securities, source_versions = _load_context(parent_partitions[0].path)
    sessions = _integer(spec.get("sessions"), "parent sessions")
    if sessions < 1 or sessions > 2000:
        raise ConversionError("parent sessions must be within 1..2000")
    source_cutoff = _string(active.get("source_cutoff"), "active source cutoff")
    active_hash = _hash_file(active_path)
    fingerprint = _hash_text(
        _canonical_json(
            {
                "active_manifest_file_hash": active_hash,
                "increment_partitions": [
                    [item.relative_path, item.expected_sha256, item.expected_rows] for item in increment_partitions
                ],
                "manifest_file_hash": manifest_hash,
                "parent_partitions": [
                    [item.relative_path, item.expected_sha256, item.expected_rows] for item in parent_partitions
                ],
            }
        )
    )
    return SourceArchive(
        root=root,
        source_fingerprint=fingerprint,
        manifest_file_hash=manifest_hash,
        active_manifest_file_hash=active_hash,
        parent_manifest_hash=_string(manifest.get("content_hash"), "parent manifest content hash"),
        active_data_hash=_string(active.get("active_data_hash"), "active data hash"),
        source_identity_hash=_string(active.get("source_identity_hash"), "source identity hash"),
        source_cutoff=source_cutoff,
        sessions=sessions,
        calendar_dates=dates,
        securities=securities,
        source_versions_json=source_versions,
        parent_partitions=parent_partitions,
        increment_partitions=increment_partitions,
        field_coverage=_field_coverage(active),
        production_authority=_boolean(active.get("production_authority"), "production authority"),
        point_in_time_parity=_boolean(active.get("point_in_time_parity"), "point-in-time parity"),
    )


def _connect(
    path: Path,
    cache_mib: int,
    *,
    read_only: bool = False,
    wal: bool = False,
) -> sqlite3.Connection:
    if read_only:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(path)
        connection.execute(f"PRAGMA journal_mode={'WAL' if wal else 'DELETE'}")
        connection.execute("PRAGMA synchronous=FULL")
    connection.execute(f"PRAGMA cache_size=-{cache_mib * 1024}")
    connection.execute("PRAGMA temp_store=FILE")
    connection.execute("PRAGMA threads=1")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _verify_database(
    path: Path,
    expected_sha256: str,
    expected_rows: int,
    table: str,
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
) -> None:
    if not path.is_file():
        raise ConversionError(f"source partition is missing: {path.name}")
    if _hash_file(path, throttle_seconds, cancellation) != expected_sha256:
        raise ConversionError(f"source partition SHA-256 mismatch: {path.name}")
    try:
        with closing(_connect(path, cache_mib, read_only=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ConversionError(f"source partition integrity check failed: {path.name}")
            count = cast(int, connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    except sqlite3.DatabaseError as exc:
        raise ConversionError(f"source partition is unreadable: {path.name}") from exc
    if count != expected_rows:
        raise ConversionError(f"source partition row count mismatch: {path.name}")


def _source_signature(source: SourceArchive) -> str:
    entries: list[tuple[str, int, int, str]] = []
    for parent_partition in source.parent_partitions:
        stat = parent_partition.path.stat()
        entries.append(
            (
                parent_partition.relative_path,
                stat.st_size,
                stat.st_mtime_ns,
                parent_partition.expected_sha256,
            )
        )
    for increment_partition in source.increment_partitions:
        stat = increment_partition.path.stat()
        entries.append(
            (
                increment_partition.relative_path,
                stat.st_size,
                stat.st_mtime_ns,
                increment_partition.expected_sha256,
            )
        )
    return _hash_text(_canonical_json(entries))


def _verify_source(
    source: SourceArchive,
    control: sqlite3.Connection,
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
    progress_sink: ProgressSink | None,
) -> None:
    signature = _source_signature(source)
    stored = control.execute("SELECT value FROM metadata WHERE key='verified_source_signature'").fetchone()
    total = len(source.parent_partitions) + len(source.increment_partitions)
    progress = _ProgressTracker("源校验", total, progress_sink)
    if stored == (signature,):
        progress.advance(total, "已缓存", force=True)
        return
    for index, partition in enumerate(source.parent_partitions, 1):
        cancellation.check()
        _verify_database(
            partition.path,
            partition.expected_sha256,
            partition.expected_rows,
            "daily_cells",
            cache_mib,
            throttle_seconds,
            cancellation,
        )
        progress.advance(1, f"文件={partition.path.name}", force=index == total)
    offset = len(source.parent_partitions)
    for index, increment_partition in enumerate(source.increment_partitions, 1):
        cancellation.check()
        _verify_database(
            increment_partition.path,
            increment_partition.expected_sha256,
            increment_partition.expected_rows,
            "records",
            cache_mib,
            throttle_seconds,
            cancellation,
        )
        progress.advance(1, f"文件={increment_partition.path.name}", force=offset + index == total)
    with control:
        control.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES ('verified_source_signature', ?)",
            (signature,),
        )


def _create_progress_database(path: Path, source: SourceArchive, cache_mib: int) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path, cache_mib)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS conversion_progress (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            relative_path TEXT NOT NULL,
            database_sha256 TEXT NOT NULL,
            logical_content_hash TEXT NOT NULL,
            physical_rows INTEGER NOT NULL,
            active_rows INTEGER NOT NULL,
            source_rows INTEGER NOT NULL,
            PRIMARY KEY (year, month)
        ) WITHOUT ROWID;
        """
    )
    existing = connection.execute("SELECT value FROM metadata WHERE key='source_fingerprint'").fetchone()
    if existing is not None and existing != (source.source_fingerprint,):
        connection.close()
        raise ConversionError("staging directory belongs to a different source archive")
    with connection:
        for key, value in (
            ("schema", _PROGRESS_SCHEMA),
            ("state", "converting"),
            ("source_fingerprint", source.source_fingerprint),
            ("manifest_file_hash", source.manifest_file_hash),
            ("active_manifest_file_hash", source.active_manifest_file_hash),
            ("source_versions", source.source_versions_json),
        ):
            connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, value))
    return connection


def _increment_dates(source: SourceArchive, cache_mib: int) -> set[str]:
    dates: set[str] = set()
    for partition in source.increment_partitions:
        try:
            with closing(_connect(partition.path, cache_mib, read_only=True)) as connection:
                rows = connection.execute(
                    "SELECT DISTINCT trade_date FROM records "
                    "WHERE field_family IN ('daily_raw', 'daily_qfq') ORDER BY trade_date"
                )
                dates.update(cast(str, row[0]) for row in rows)
        except sqlite3.DatabaseError as exc:
            raise ConversionError(f"increment dates are unreadable: {partition.path.name}") from exc
    return dates


def _source_rows_from(source: SourceArchive, start: str, cache_mib: int) -> int:
    total = 0
    try:
        for parent_partition in source.parent_partitions:
            with closing(_connect(parent_partition.path, cache_mib, read_only=True)) as connection:
                total += cast(
                    int,
                    connection.execute(
                        "SELECT COUNT(*) FROM daily_cells WHERE trade_date>=?",
                        (start,),
                    ).fetchone()[0],
                )
        for increment_partition in source.increment_partitions:
            with closing(_connect(increment_partition.path, cache_mib, read_only=True)) as connection:
                total += cast(
                    int,
                    connection.execute(
                        "SELECT COUNT(*) FROM records WHERE trade_date>=?",
                        (start,),
                    ).fetchone()[0],
                )
    except sqlite3.DatabaseError as exc:
        raise ConversionError("source row counts are unreadable") from exc
    return total


def _months(dates: Sequence[str]) -> tuple[tuple[int, int], ...]:
    return tuple(sorted({(int(day[:4]), int(day[5:7])) for day in dates}))


def _month_bounds(year: int, month: int) -> tuple[str, str]:
    start = f"{year:04d}-{month:02d}-01"
    if month == 12:
        end = f"{year + 1:04d}-01-01"
    else:
        end = f"{year:04d}-{month + 1:02d}-01"
    return start, end


def _load_industries(source: SourceArchive, cache_mib: int) -> dict[str, tuple[IndustryInterval, ...]]:
    result: dict[tuple[str, str], IndustryInterval] = {}
    try:
        with closing(_connect(source.parent_partitions[0].path, cache_mib, read_only=True)) as connection:
            rows = connection.execute(
                "SELECT code, effective_from, effective_to, industry, classification, content_hash "
                "FROM industry_intervals ORDER BY code, effective_from"
            )
            for code, effective_from, effective_to, industry, classification, content_hash in rows:
                key = (cast(str, code), cast(str, effective_from))
                result[key] = IndustryInterval(
                    key[1],
                    cast(str | None, effective_to),
                    cast(str, industry),
                    cast(str, classification),
                    cast(str, content_hash),
                )
        for partition in source.increment_partitions:
            with closing(_connect(partition.path, cache_mib, read_only=True)) as connection:
                rows = connection.execute(
                    "SELECT code, trade_date, payload_json, content_hash FROM records "
                    "WHERE field_family='industry' ORDER BY code, trade_date"
                )
                for code, effective_from, payload_json, content_hash in rows:
                    payload = _object(json.loads(cast(str, payload_json)), "increment industry")
                    _validate_overlay_identity(payload, cast(str, code), cast(str, effective_from))
                    effective_to = payload.get("effective_to")
                    if effective_to is not None and not isinstance(effective_to, str):
                        raise ConversionError("increment industry effective_to is invalid")
                    interval = IndustryInterval(
                        cast(str, effective_from),
                        effective_to,
                        _string(payload.get("industry"), "increment industry"),
                        _string(payload.get("classification"), "increment industry classification"),
                        cast(str, content_hash),
                    )
                    key = (cast(str, code), cast(str, effective_from))
                    previous = result.get(key)
                    if previous is not None and previous != interval:
                        raise ConversionError("industry interval identity conflicts across source layers")
                    result[key] = interval
    except (sqlite3.DatabaseError, json.JSONDecodeError, TypeError) as exc:
        raise ConversionError("source industry intervals are unreadable") from exc
    by_code: dict[str, list[IndustryInterval]] = {}
    for (code, _effective_from), interval in sorted(result.items()):
        by_code.setdefault(code, []).append(interval)
    return {code: tuple(items) for code, items in by_code.items()}


def _industry_for(
    intervals_by_code: dict[str, tuple[IndustryInterval, ...]], code: str, day: str
) -> IndustryInterval | None:
    intervals = intervals_by_code.get(code, ())
    if not intervals:
        return None
    starts = [item.effective_from for item in intervals]
    index = bisect.bisect_right(starts, day) - 1
    if index < 0:
        return None
    candidate = intervals[index]
    return candidate if candidate.effective_to is None or day < candidate.effective_to else None


def _payload_json(value: object, label: str) -> str | None:
    if value is None:
        return None
    payload = _object(value, label)
    return _canonical_json(payload)


def _record_hash(record: DailyRecord) -> str:
    value = _monthly_revision(record).revision_id
    if not isinstance(value, str):
        raise ConversionError("converted history revision identity is invalid")
    return value


def _monthly_revision(record: DailyRecord) -> HistoryMonthlyRevision:
    status = "unknown_missing" if record.status == "unavailable" else record.status
    payload = {
        "first_seen_sequence": record.sync_sequence,
        "board": _normalized_board(record.board),
        "cell": {
            "code": record.code,
            "trade_date": record.trade_date,
            "status": status,
            "unadjusted": json.loads(record.raw_payload_json) if record.raw_payload_json is not None else None,
            "qfq": json.loads(record.qfq_payload_json) if record.qfq_payload_json is not None else None,
        },
        "is_st": bool(record.is_st) if record.is_st is not None else None,
        "industry": record.industry,
        "industry_classification": record.industry_classification,
    }
    try:
        return decode_history_monthly_revision(_canonical_json(payload))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConversionError("converted history row does not satisfy the monthly contract") from exc


def _status(raw_json: str | None, qfq_json: str | None, fallback: str | None = None) -> str:
    if raw_json is not None and qfq_json is not None:
        raw = _object(json.loads(raw_json), "raw side")
        qfq = _object(json.loads(qfq_json), "qfq side")
        if raw.get("trading_status") == "suspended" and qfq.get("trading_status") == "suspended":
            return "supplier_marked_suspended"
        return "complete"
    if raw_json is not None:
        return "qfq_missing"
    if qfq_json is not None:
        return "unadjusted_missing"
    return fallback or "unknown_missing"


def _parent_record(
    code: str,
    day: str,
    payload_json: str,
    is_st: int | None,
    is_st_hash: str | None,
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
) -> DailyRecord:
    try:
        payload = _object(json.loads(payload_json), "parent daily cell")
    except json.JSONDecodeError as exc:
        raise ConversionError("parent daily cell JSON is invalid") from exc
    if payload.get("code") != code or payload.get("trade_date") != day:
        raise ConversionError("parent daily cell identity does not match its SQLite key")
    raw_json = _payload_json(payload.get("unadjusted"), "parent raw side")
    qfq_json = _payload_json(payload.get("qfq"), "parent qfq side")
    interval = _industry_for(industries, code, day)
    record = DailyRecord(
        trade_date=day,
        code=code,
        sync_sequence=1,
        board=board_by_code.get(code, "unknown"),
        status=_status(raw_json, qfq_json, cast(str | None, payload.get("status"))),
        raw_payload_json=raw_json,
        qfq_payload_json=qfq_json,
        is_st=is_st,
        industry=interval.industry if interval is not None else None,
        industry_classification=interval.classification if interval is not None else None,
        raw_content_hash=_hash_text(raw_json) if raw_json is not None else None,
        qfq_content_hash=_hash_text(qfq_json) if qfq_json is not None else None,
        is_st_content_hash=is_st_hash,
        industry_content_hash=interval.content_hash if interval is not None else None,
        row_hash="",
    )
    return record


_INSERT_RECORD = """
    INSERT OR IGNORE INTO daily_records(
        trade_date, code, revision_id, first_seen_sequence, board, payload_json, content_hash
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""
_INSERT_OBSERVATION = """
    INSERT INTO daily_observations(trade_date, code, sync_sequence, revision_id)
    VALUES (?, ?, ?, ?)
"""


def _persistence_values(record: DailyRecord) -> tuple[tuple[object, ...], tuple[object, ...]]:
    revision = _monthly_revision(record)
    return (
        (
            revision.trade_date.isoformat(),
            revision.code,
            revision.revision_id,
            revision.first_seen_sequence,
            revision.board,
            encode_history_monthly_revision(revision),
            revision.content_hash,
        ),
        (
            revision.trade_date.isoformat(),
            revision.code,
            record.sync_sequence,
            revision.revision_id,
        ),
    )


def _pause(throttle_seconds: float, cancellation: _Cancellation) -> None:
    cancellation.check()
    if throttle_seconds > 0:
        time.sleep(throttle_seconds)


def _copy_parent_month(
    target: sqlite3.Connection,
    source: SourceArchive,
    start: str,
    end: str,
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    batch_size: int,
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
    progress: _ProgressTracker,
    current: str,
) -> int:
    copied = 0
    uncommitted = 0
    for partition in source.parent_partitions:
        try:
            with closing(_connect(partition.path, cache_mib, read_only=True)) as connection:
                cursor = connection.execute(
                    "SELECT cells.code, cells.trade_date, cells.payload_json, facts.is_st, facts.content_hash "
                    "FROM daily_cells AS cells LEFT JOIN daily_facts AS facts "
                    "ON facts.code=cells.code AND facts.trade_date=cells.trade_date "
                    "WHERE cells.trade_date>=? AND cells.trade_date<? "
                    "ORDER BY cells.trade_date, cells.code",
                    (start, end),
                )
                while rows := cursor.fetchmany(batch_size):
                    records = [
                        _parent_record(
                            cast(str, code),
                            cast(str, day),
                            cast(str, payload_json),
                            cast(int | None, is_st),
                            cast(str | None, fact_hash),
                            board_by_code,
                            industries,
                        )
                        for code, day, payload_json, is_st, fact_hash in rows
                    ]
                    values = [_persistence_values(item) for item in records]
                    target.executemany(_INSERT_RECORD, (item[0] for item in values))
                    target.executemany(_INSERT_OBSERVATION, (item[1] for item in values))
                    copied += len(rows)
                    uncommitted += len(rows)
                    if uncommitted >= 8192:
                        target.commit()
                        uncommitted = 0
                    progress.advance(len(rows), current)
                    _pause(throttle_seconds, cancellation)
        except sqlite3.DatabaseError as exc:
            raise ConversionError(f"parent month scan failed: {partition.path.name}") from exc
    target.commit()
    return copied


def _active_record(connection: sqlite3.Connection, code: str, day: str) -> DailyRecord | None:
    row = connection.execute(
        "SELECT records.payload_json FROM daily_observations AS observations "
        "JOIN daily_records AS records ON records.trade_date=observations.trade_date "
        "AND records.code=observations.code AND records.revision_id=observations.revision_id "
        "WHERE observations.trade_date=? AND observations.code=? "
        "ORDER BY observations.sync_sequence DESC LIMIT 1",
        (day, code),
    ).fetchone()
    if row is None:
        return None
    try:
        revision = decode_history_monthly_revision(cast(str, row[0]))
        payload = _object(json.loads(cast(str, row[0])), "monthly revision")
        cell = _object(payload["cell"], "monthly cell")
        raw_json = _payload_json(cell.get("unadjusted"), "monthly raw side")
        qfq_json = _payload_json(cell.get("qfq"), "monthly qfq side")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ConversionError("converted monthly revision is unreadable") from exc
    record = DailyRecord(
        revision.trade_date.isoformat(),
        revision.code,
        revision.first_seen_sequence,
        revision.board,
        revision.cell.status,
        raw_json,
        qfq_json,
        int(revision.is_st) if revision.is_st is not None else None,
        revision.industry,
        revision.industry_classification,
        _hash_text(raw_json) if raw_json is not None else None,
        _hash_text(qfq_json) if qfq_json is not None else None,
        None,
        None,
        revision.revision_id,
    )
    return record


def _validate_overlay_identity(payload: dict[str, object], code: str, day: str) -> None:
    if payload.get("code") != code or payload.get("trade_date") != day:
        raise ConversionError("increment record identity does not match its SQLite key")


def _overlay_record(
    base: DailyRecord | None,
    code: str,
    day: str,
    rows: Sequence[tuple[str, str, str]],
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    sync_sequence: int,
) -> DailyRecord | None:
    interval = _industry_for(industries, code, day)
    record = base or DailyRecord(
        trade_date=day,
        code=code,
        sync_sequence=sync_sequence,
        board=board_by_code.get(code, "unknown"),
        status="unknown_missing",
        raw_payload_json=None,
        qfq_payload_json=None,
        is_st=None,
        industry=interval.industry if interval is not None else None,
        industry_classification=interval.classification if interval is not None else None,
        raw_content_hash=None,
        qfq_content_hash=None,
        is_st_content_hash=None,
        industry_content_hash=interval.content_hash if interval is not None else None,
        row_hash="",
    )
    if base is not None and interval is not None:
        record = replace(
            record,
            industry=interval.industry,
            industry_classification=interval.classification,
            industry_content_hash=interval.content_hash,
        )
    has_daily_data = base is not None
    for family, payload_json, content_hash in rows:
        try:
            payload = _object(json.loads(payload_json), "increment record")
        except json.JSONDecodeError as exc:
            raise ConversionError("increment record JSON is invalid") from exc
        _validate_overlay_identity(payload, code, day)
        canonical = _canonical_json(payload)
        if family == "daily_raw":
            record = replace(record, raw_payload_json=canonical, raw_content_hash=content_hash)
            has_daily_data = True
        elif family == "daily_qfq":
            record = replace(record, qfq_payload_json=canonical, qfq_content_hash=content_hash)
            has_daily_data = True
        elif family == "is_st":
            value = payload.get("is_st")
            if not isinstance(value, bool):
                raise ConversionError("increment is_st payload is invalid")
            record = replace(record, is_st=int(value), is_st_content_hash=content_hash)
        elif family == "industry":
            industry = payload.get("industry")
            classification = payload.get("classification")
            if (
                not isinstance(industry, str)
                or not industry
                or not isinstance(classification, str)
                or not classification
            ):
                raise ConversionError("increment industry payload is invalid")
            record = replace(
                record,
                industry=industry,
                industry_classification=classification,
                industry_content_hash=content_hash,
            )
        elif family not in _SOURCE_FAMILIES:
            raise ConversionError("increment field family is unsupported")
    if not has_daily_data:
        return None
    record = replace(
        record,
        sync_sequence=sync_sequence,
        status=_status(record.raw_payload_json, record.qfq_payload_json, record.status),
        row_hash="",
    )
    return replace(record, row_hash=_record_hash(record))


def _copy_increment_month(
    target: sqlite3.Connection,
    source: SourceArchive,
    start: str,
    end: str,
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    batch_size: int,
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
    progress: _ProgressTracker,
    current: str,
) -> int:
    copied = 0
    uncommitted = 0
    for partition in source.increment_partitions:
        try:
            with closing(_connect(partition.path, cache_mib, read_only=True)) as connection:
                cursor = connection.execute(
                    "SELECT code, trade_date, field_family, payload_json, content_hash FROM records "
                    "WHERE trade_date>=? AND trade_date<? ORDER BY code, trade_date, field_family",
                    (start, end),
                )
                current_key: tuple[str, str] | None = None
                grouped: list[tuple[str, str, str]] = []
                since_pause = 0
                for code_value, day_value, family_value, payload_json_value, content_hash_value in cursor:
                    key = (cast(str, code_value), cast(str, day_value))
                    if current_key is not None and key != current_key:
                        _write_overlay(target, current_key, grouped, board_by_code, industries)
                        grouped = []
                    current_key = key
                    grouped.append(
                        (cast(str, family_value), cast(str, payload_json_value), cast(str, content_hash_value))
                    )
                    since_pause += 1
                    copied += 1
                    uncommitted += 1
                    progress.advance(1, current)
                    if uncommitted >= 8192:
                        target.commit()
                        uncommitted = 0
                    if since_pause >= batch_size:
                        _pause(throttle_seconds, cancellation)
                        since_pause = 0
                if current_key is not None:
                    _write_overlay(target, current_key, grouped, board_by_code, industries)
                _pause(throttle_seconds, cancellation)
        except sqlite3.DatabaseError as exc:
            raise ConversionError(f"increment month scan failed: {partition.path.name}") from exc
    target.commit()
    return copied


def _write_overlay(
    connection: sqlite3.Connection,
    key: tuple[str, str],
    rows: Sequence[tuple[str, str, str]],
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    *,
    sync_sequence: int = 2,
) -> bool:
    code, day = key
    base = _active_record(connection, code, day)
    record = _overlay_record(base, code, day, rows, board_by_code, industries, sync_sequence)
    if record is not None and (base is None or record.row_hash != base.row_hash):
        record_values, observation_values = _persistence_values(record)
        connection.execute(_INSERT_RECORD, record_values)
        connection.execute(_INSERT_OBSERVATION, observation_values)
        return True
    return False


def _logical_hash(connection: sqlite3.Connection) -> tuple[str, int]:
    digest = hashlib.sha256()
    count = 0
    rows = connection.execute(
        "SELECT records.revision_id FROM daily_observations AS observations "
        "JOIN daily_records AS records ON records.trade_date=observations.trade_date "
        "AND records.code=observations.code AND records.revision_id=observations.revision_id "
        "WHERE observations.sync_sequence=(SELECT MAX(candidate.sync_sequence) "
        "FROM daily_observations AS candidate WHERE candidate.trade_date=observations.trade_date "
        "AND candidate.code=observations.code) ORDER BY observations.trade_date, observations.code"
    )
    for (revision_id,) in rows:
        digest.update(cast(str, revision_id).encode("ascii"))
        digest.update(b"\n")
        count += 1
    return digest.hexdigest(), count


def _missing_gap_requests(
    staging: Path,
    results: Sequence[PartitionResult],
    cache_mib: int,
    progress_sink: ProgressSink | None = None,
) -> tuple[BaoStockGapRequest, ...]:
    grouped: dict[tuple[str, str], set[date]] = {}
    progress = _ProgressTracker("缺口扫描", sum(item.active_rows for item in results), progress_sink)
    for result in results:
        path = _safe_child(staging, result.relative_path, "converted partition")
        with closing(_connect(path, cache_mib, read_only=True)) as connection:
            cursor = connection.execute(
                "SELECT records.payload_json FROM daily_observations AS observations "
                "JOIN daily_records AS records ON records.trade_date=observations.trade_date "
                "AND records.code=observations.code AND records.revision_id=observations.revision_id "
                "WHERE observations.sync_sequence=(SELECT MAX(candidate.sync_sequence) "
                "FROM daily_observations AS candidate WHERE candidate.trade_date=observations.trade_date "
                "AND candidate.code=observations.code) ORDER BY observations.code, observations.trade_date"
            )
            while rows := cursor.fetchmany(512):
                for (payload_json,) in rows:
                    try:
                        revision = decode_history_monthly_revision(cast(str, payload_json))
                    except (TypeError, ValueError) as exc:
                        raise ConversionError("converted monthly revision is unreadable") from exc
                    missing = (
                        ("daily_raw", revision.cell.unadjusted),
                        ("daily_qfq", revision.cell.qfq),
                        ("is_st", revision.is_st),
                    )
                    for family, value in missing:
                        if value is None:
                            grouped.setdefault((revision.code, family), set()).add(revision.trade_date)
                progress.advance(len(rows), f"月份={result.year:04d}-{result.month:02d}")
        _remove_empty_sqlite_sidecars(path)
    return tuple(
        BaoStockGapRequest(code, cast(BaoStockGapFamily, family), tuple(sorted(days)))
        for (code, family), days in sorted(grouped.items())
    )


def _apply_gap_result(
    staging: Path,
    results: Sequence[PartitionResult],
    result: BaoStockGapResult,
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
) -> tuple[tuple[PartitionResult, ...], int]:
    by_month: dict[tuple[int, int], dict[tuple[str, str], list[tuple[str, str, str]]]] = {}
    for item in result.records:
        month_key = (item.trade_date.year, item.trade_date.month)
        row_key = (item.code, item.trade_date.isoformat())
        by_month.setdefault(month_key, {}).setdefault(row_key, []).append(
            (item.family, item.payload_json, item.content_hash)
        )
    refreshed: list[PartitionResult] = []
    written = 0
    for previous in results:
        additions = by_month.get((previous.year, previous.month), {})
        if not additions:
            refreshed.append(previous)
            continue
        source_path = _safe_child(staging, previous.relative_path, "converted partition")
        pending = staging / "partitions" / f"{previous.year:04d}" / f".{previous.month:02d}.supplement.pending.sqlite3"
        _remove_pending(pending)
        shutil.copy2(source_path, pending)
        with closing(_connect(pending, cache_mib, wal=True)) as connection:
            for key, rows in sorted(additions.items()):
                written += int(
                    _write_overlay(
                        connection,
                        key,
                        rows,
                        board_by_code,
                        industries,
                        sync_sequence=3,
                    )
                )
            connection.commit()
            logical_hash, active_rows = _logical_hash(connection)
            physical_rows = cast(int, connection.execute("SELECT COUNT(*) FROM daily_records").fetchone()[0])
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ConversionError(
                    f"supplemented month failed integrity check: {previous.year:04d}-{previous.month:02d}"
                )
        reference = SQLiteHistoryMonthPartitionRepository(pending, previous.year, previous.month).seal()
        refreshed.append(
            replace(
                previous,
                relative_path=reference.relative_path,
                database_sha256=_hash_file(staging / reference.relative_path, throttle_seconds, cancellation),
                logical_content_hash=logical_hash,
                physical_rows=physical_rows,
                active_rows=active_rows,
            )
        )
    return tuple(refreshed), written


def _supplement_missing_fields(
    staging: Path,
    results: Sequence[PartitionResult],
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
    progress_sink: ProgressSink | None,
    provider: SupplementProvider,
) -> tuple[tuple[PartitionResult, ...], int, int]:
    requests = _missing_gap_requests(staging, results, cache_mib, progress_sink)
    progress = _ProgressTracker("补缺下载", len(requests), progress_sink)
    if not requests:
        progress.emit("无可下载缺口", force=True)
        return tuple(results), 0, 0
    requested_keys = {(item.code, day, item.family) for item in requests for day in item.trade_dates}
    completed = 0

    def report(value: int, _total: int, current: str) -> None:
        nonlocal completed
        progress.advance(value - completed, f"请求={current}", force=value == len(requests))
        completed = value

    try:
        fetched = provider(
            requests,
            cancel_requested=lambda: cancellation.requested,
            progress=report,
        )
    except BaoStockGapSupplierError as exc:
        raise ConversionError("downloadable field supplementation failed") from exc
    result_keys = {(item.code, item.trade_date, item.family) for item in fetched.records}
    unavailable_keys = {(item.code, item.trade_date, item.family) for item in fetched.unavailable}
    if result_keys | unavailable_keys != requested_keys:
        raise ConversionError("gap supplier result does not match requested identities")
    refreshed, written = _apply_gap_result(
        staging,
        results,
        fetched,
        board_by_code,
        industries,
        cache_mib,
        throttle_seconds,
        cancellation,
    )
    return refreshed, written, len(unavailable_keys)


def _remove_pending(path: Path) -> None:
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        if candidate.is_file():
            candidate.unlink()


def _convert_month(
    source: SourceArchive,
    staging: Path,
    year: int,
    month: int,
    active_start: str,
    board_by_code: dict[str, str],
    industries: dict[str, tuple[IndustryInterval, ...]],
    batch_size: int,
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
    progress: _ProgressTracker,
) -> PartitionResult:
    pending = staging / "partitions" / f"{year:04d}" / f".{month:02d}.pending.sqlite3"
    pending.parent.mkdir(parents=True, exist_ok=True)
    _remove_pending(pending)
    start, end = _month_bounds(year, month)
    start = max(start, active_start)
    current = f"月份={year:04d}-{month:02d}"
    try:
        repository = SQLiteHistoryMonthPartitionRepository(pending, year, month)
        repository.initialize()
        with closing(_connect(pending, cache_mib, wal=True)) as connection:
            parent_rows = _copy_parent_month(
                connection,
                source,
                start,
                end,
                board_by_code,
                industries,
                batch_size,
                cache_mib,
                throttle_seconds,
                cancellation,
                progress,
                current,
            )
            increment_rows = _copy_increment_month(
                connection,
                source,
                start,
                end,
                board_by_code,
                industries,
                batch_size,
                cache_mib,
                throttle_seconds,
                cancellation,
                progress,
                current,
            )
            logical_hash, active_rows = _logical_hash(connection)
            physical_rows = cast(int, connection.execute("SELECT COUNT(*) FROM daily_records").fetchone()[0])
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise ConversionError(f"converted month failed integrity check: {year:04d}-{month:02d}")
        reference = repository.seal()
        final_path = staging / reference.relative_path
        database_sha256 = _hash_file(final_path, throttle_seconds, cancellation)
        if database_sha256 != reference.sha256:
            raise ConversionError(f"sealed month hash changed: {year:04d}-{month:02d}")
        return PartitionResult(
            year,
            month,
            reference.relative_path,
            database_sha256,
            logical_hash,
            physical_rows,
            active_rows,
            parent_rows + increment_rows,
        )
    except BaseException:
        _remove_pending(pending)
        raise


def _completed_partition(
    control: sqlite3.Connection, staging: Path, year: int, month: int, throttle_seconds: float
) -> PartitionResult | None:
    row = control.execute(
        "SELECT relative_path, database_sha256, logical_content_hash, physical_rows, active_rows, source_rows "
        "FROM conversion_progress WHERE year=? AND month=?",
        (year, month),
    ).fetchone()
    if row is None:
        return None
    relative, expected_hash, logical_hash, physical_rows, active_rows, source_rows = cast(tuple[Any, ...], row)
    path = _safe_child(staging, cast(str, relative), "completed partition")
    if not path.is_file() or _hash_file(path, throttle_seconds) != expected_hash:
        raise ConversionError(f"completed staging month changed: {year:04d}-{month:02d}")
    return PartitionResult(
        year,
        month,
        cast(str, relative),
        cast(str, expected_hash),
        cast(str, logical_hash),
        cast(int, physical_rows),
        cast(int, active_rows),
        cast(int, source_rows),
    )


def _save_progress(control: sqlite3.Connection, result: PartitionResult) -> None:
    with control:
        control.execute(
            "INSERT OR REPLACE INTO conversion_progress VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                result.year,
                result.month,
                result.relative_path,
                result.database_sha256,
                result.logical_content_hash,
                result.physical_rows,
                result.active_rows,
                result.source_rows,
            ),
        )


def _normalized_board(value: str) -> HistorySecurityBoard:
    if value == "growth":
        return "chinext"
    if value in {"main", "chinext", "star"}:
        return cast(HistorySecurityBoard, value)
    raise ConversionError(f"unsupported security board: {value}")


def _finalize_control(
    staging: Path,
    source: SourceArchive,
    all_dates: Sequence[str],
    results: Sequence[PartitionResult],
    snapshot_sequence: int,
) -> tuple[str, tuple[str, ...]]:
    active_dates = tuple(sorted(set(all_dates))[-source.sessions :])
    if not active_dates or active_dates[-1] != source.source_cutoff:
        raise ConversionError("active calendar does not end at the active source cutoff")
    cutoff = date.fromisoformat(source.source_cutoff)
    observed_at = datetime.combine(cutoff, datetime_time(15, 0), tzinfo=_SHANGHAI)
    source_identity = HistorySourceIdentity(
        "baostock",
        f"legacy_daily.{source.source_fingerprint}",
        "sealed_parent_increment",
        observed_at,
    )
    calendar = HistoryCalendarIdentity(
        tuple(date.fromisoformat(item) for item in active_dates),
        source_identity.content_hash,
    )
    universe = HistoryUniverseIdentity(
        tuple(
            HistorySecurityIdentity(
                item.code,
                item.name,
                _normalized_board(item.board),
                date.fromisoformat(item.listed_on),
                date.fromisoformat(item.delisted_on) if item.delisted_on is not None else None,
            )
            for item in source.securities
        ),
        source_identity.content_hash,
    )
    label_cutoff = calendar.open_dates[-2] if len(calendar.open_dates) > 1 else calendar.open_dates[-1]
    snapshot = HistoryActiveSnapshot(
        snapshot_sequence,
        cutoff,
        label_cutoff,
        calendar.content_hash,
        universe.content_hash,
        source_identity.content_hash,
        tuple(
            HistorySnapshotPartition(item.relative_path, item.database_sha256, item.physical_rows) for item in results
        ),
    )
    identity_suffix = source.source_fingerprint[:24]
    checkpoint = HistorySyncCheckpoint(
        f"conversion.{identity_suffix}",
        snapshot_sequence,
        "completed",
        observed_at,
        len(results),
        len(results),
        None,
    )
    missing_rows = sum(item[3] for item in source.field_coverage)
    due_state = HistoryTrainingDueState(
        f"conversion.{identity_suffix}",
        "data_incomplete" if missing_rows else "initial_training_required",
        None,
        label_cutoff,
        0,
        False,
        observed_at,
    )
    pending = staging / "control.sqlite3.pending"
    _remove_pending(pending)
    try:
        repository = SQLiteHistoryControlRepository(pending)
        repository.initialize()
        repository.save_source(source_identity)
        repository.save_calendar(calendar)
        repository.save_universe(universe)
        repository.save_checkpoint(checkpoint)
        repository.save_due_state(due_state)
        repository.publish_snapshot(snapshot)
        if repository.load_state().active_snapshot != snapshot:
            raise ConversionError("converted control state did not round-trip")
        with closing(sqlite3.connect(pending)) as connection:
            checkpoint_result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint_result is None or checkpoint_result[0] != 0:
                raise ConversionError("converted control WAL checkpoint did not complete")
        _fsync_file(pending)
        os.replace(pending, staging / "control.sqlite3")
        _fsync_directory(staging)
    except (HistoryControlError, ValueError) as exc:
        raise ConversionError("converted control database is invalid") from exc
    finally:
        _remove_pending(pending)
    return snapshot.content_hash, active_dates


def _read_completed_summary(
    target: Path,
    source_fingerprint: str,
    batch_size: int,
    cache_mib: int,
    throttle_ms: int,
    progress_sink: ProgressSink | None,
) -> ConversionSummary | None:
    control_path = target / "control.sqlite3"
    if not control_path.is_file():
        return None
    try:
        state = SQLiteHistoryControlRepository(control_path).load_state()
        _remove_empty_sqlite_sidecars(control_path)
        snapshot = state.active_snapshot
        if snapshot is None:
            return None
        source = next(item for item in state.sources if item.content_hash == snapshot.source_identity_hash)
        if source.dataset != f"legacy_daily.{source_fingerprint}":
            raise ConversionError("target already exists for a different source archive")
        calendar = next(item for item in state.calendars if item.content_hash == snapshot.calendar_hash)
        physical_rows = 0
        active_rows = 0
        remaining_gaps = 0
        progress = _ProgressTracker("完成态统计", len(snapshot.partitions), progress_sink)
        for partition in snapshot.partitions:
            path = _safe_child(target, partition.relative_path, "active partition")
            with closing(_connect(path, cache_mib, read_only=True)) as connection:
                summary = connection.execute(
                    "WITH latest AS ("
                    "SELECT trade_date, code, MAX(sync_sequence) AS sync_sequence FROM daily_observations "
                    "GROUP BY trade_date, code"
                    ") SELECT COUNT(*), "
                    "COALESCE(SUM(json_extract(records.payload_json, '$.cell.unadjusted') IS NULL), 0), "
                    "COALESCE(SUM(json_extract(records.payload_json, '$.cell.qfq') IS NULL), 0), "
                    "COALESCE(SUM(json_extract(records.payload_json, '$.is_st') IS NULL), 0) "
                    "FROM latest JOIN daily_observations AS observations "
                    "ON observations.trade_date=latest.trade_date AND observations.code=latest.code "
                    "AND observations.sync_sequence=latest.sync_sequence "
                    "JOIN daily_records AS records ON records.trade_date=observations.trade_date "
                    "AND records.code=observations.code AND records.revision_id=observations.revision_id"
                ).fetchone()
                if summary is None:
                    raise ConversionError("completed monthly partition summary is unavailable")
                month_active_rows, missing_raw, missing_qfq, missing_is_st = cast(tuple[int, int, int, int], summary)
                physical_rows += partition.row_count
                active_rows += month_active_rows
                remaining_gaps += missing_raw + missing_qfq + missing_is_st
            _remove_empty_sqlite_sidecars(path)
            progress.advance(1, partition.relative_path, force=True)
        return ConversionSummary(
            "already_current",
            len(snapshot.partitions),
            physical_rows,
            active_rows,
            len(calendar.open_dates),
            snapshot.data_cutoff.isoformat(),
            snapshot.content_hash,
            batch_size,
            cache_mib,
            throttle_ms,
            0,
            remaining_gaps,
        )
    except ConversionError:
        raise
    except (HistoryControlError, StopIteration, TypeError, sqlite3.DatabaseError) as exc:
        raise ConversionError("existing target control database is invalid") from exc


def _read_completed_metadata(
    target: Path,
    source: SourceArchive,
    batch_size: int,
    cache_mib: int,
    throttle_ms: int,
    *,
    remaining_gaps: int,
) -> ConversionSummary | None:
    control_path = target / "control.sqlite3"
    if not control_path.is_file():
        return None
    try:
        state = SQLiteHistoryControlRepository(control_path).load_state()
        snapshot = state.active_snapshot
        if snapshot is None:
            return None
        identity = next(item for item in state.sources if item.content_hash == snapshot.source_identity_hash)
        if identity.dataset != f"legacy_daily.{source.source_fingerprint}":
            raise ConversionError("target already exists for a different source archive")
        calendar = next(item for item in state.calendars if item.content_hash == snapshot.calendar_hash)
        raw_coverage = next(item for item in source.field_coverage if item[0] == "daily_raw")
        return ConversionSummary(
            "already_current",
            len(snapshot.partitions),
            sum(item.row_count for item in snapshot.partitions),
            raw_coverage[1] + raw_coverage[2],
            len(calendar.open_dates),
            snapshot.data_cutoff.isoformat(),
            snapshot.content_hash,
            batch_size,
            cache_mib,
            throttle_ms,
            0,
            remaining_gaps,
        )
    except ConversionError:
        raise
    except (HistoryControlError, StopIteration, TypeError) as exc:
        raise ConversionError("existing target control metadata is invalid") from exc


def _source_failed_qfq_requests(source: SourceArchive, cache_mib: int) -> tuple[BaoStockGapRequest, ...]:
    grouped: dict[str, set[date]] = {}
    try:
        for partition in source.increment_partitions:
            with closing(_connect(partition.path, cache_mib, read_only=True)) as connection:
                rows = connection.execute(
                    "SELECT code, trade_date FROM checkpoints WHERE field_family='daily_qfq' "
                    "AND state='failed' AND error_code='supplier_adjustment_unavailable' "
                    "ORDER BY code, trade_date"
                )
                for code_value, day_value in rows:
                    code = cast(str, code_value)
                    grouped.setdefault(code, set()).add(date.fromisoformat(cast(str, day_value)))
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise ConversionError("source qfq failure checkpoints are unreadable") from exc
    return tuple(BaoStockGapRequest(code, "daily_qfq", tuple(sorted(days))) for code, days in sorted(grouped.items()))


def _fetch_completed_gaps(
    requests: Sequence[BaoStockGapRequest],
    cancellation: _Cancellation,
    progress_sink: ProgressSink | None,
    provider: SupplementProvider,
) -> BaoStockGapResult:
    progress = _ProgressTracker("已发布归档补缺核验", len(requests), progress_sink)
    requested_keys = {(item.code, day, item.family) for item in requests for day in item.trade_dates}
    completed = 0

    def report(value: int, _total: int, current: str) -> None:
        nonlocal completed
        progress.advance(value - completed, f"请求={current}", force=value == len(requests))
        completed = value

    try:
        fetched = provider(
            requests,
            cancel_requested=lambda: cancellation.requested,
            progress=report,
        )
    except BaoStockGapSupplierError as exc:
        raise ConversionError("completed archive qfq audit failed") from exc
    result_keys = {(item.code, item.trade_date, item.family) for item in fetched.records}
    unavailable_keys = {(item.code, item.trade_date, item.family) for item in fetched.unavailable}
    if result_keys | unavailable_keys != requested_keys:
        raise ConversionError("completed archive gap result does not match requested identities")
    if any(item.reason != "supplier_adjustment_unavailable" for item in fetched.unavailable):
        raise ConversionError("completed archive contains a qfq gap without an official reconstruction basis")
    return fetched


def _latest_revisions_for_codes(
    target: Path,
    snapshot: HistoryActiveSnapshot,
    codes: Sequence[str],
    first_date: date,
    cache_mib: int,
) -> tuple[dict[str, tuple[HistoryMonthlyRevision, ...]], int]:
    grouped: dict[str, list[HistoryMonthlyRevision]] = {code: [] for code in codes}
    maximum_sequence = max(snapshot.sequence, 2)
    placeholders = ",".join("?" for _item in codes)
    parameters = (*codes, first_date.isoformat())
    for reference in snapshot.partitions:
        parts = PurePosixPath(reference.relative_path).parts
        partition_month = date(int(parts[1]), int(PurePosixPath(parts[2]).stem), 1)
        if partition_month < first_date.replace(day=1):
            continue
        path = _safe_child(target, reference.relative_path, "active partition")
        with closing(_connect(path, cache_mib, read_only=True)) as connection:
            rows = connection.execute(
                "SELECT records.payload_json FROM daily_observations AS observations "
                "JOIN daily_records AS records ON records.trade_date=observations.trade_date "
                "AND records.code=observations.code AND records.revision_id=observations.revision_id "
                f"WHERE observations.code IN ({placeholders}) AND observations.trade_date>=? "
                "AND observations.sync_sequence=(SELECT MAX(candidate.sync_sequence) "
                "FROM daily_observations AS candidate WHERE candidate.trade_date=observations.trade_date "
                "AND candidate.code=observations.code) ORDER BY observations.code, observations.trade_date",
                parameters,
            )
            for (payload_json,) in rows:
                try:
                    revision = decode_history_monthly_revision(cast(str, payload_json))
                except (TypeError, ValueError) as exc:
                    raise ConversionError("active monthly revision is unreadable during qfq repair") from exc
                grouped[revision.code].append(revision)
        _remove_empty_sqlite_sidecars(path)
    return {code: tuple(values) for code, values in grouped.items()}, maximum_sequence


_QFQ_FACTOR_QUANTUM = Decimal("0.000001")


def _side_factor(side: BaoStockDailySide, raw: BaoStockDailySide) -> Decimal:
    if side.close_price is None or raw.close_price is None or side.close_price <= 0 or raw.close_price <= 0:
        raise ConversionError("qfq reconstruction anchor has no positive close price")
    try:
        return (Decimal(str(side.close_price)) / Decimal(str(raw.close_price))).quantize(
            _QFQ_FACTOR_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ConversionError("qfq reconstruction anchor factor is invalid") from exc


def _scaled_price(value: float | None, factor: Decimal) -> float | None:
    return None if value is None else float(Decimal(str(value)) * factor)


def _reconstructed_qfq_payload(revision: HistoryMonthlyRevision, factor: Decimal) -> str:
    raw = revision.cell.unadjusted
    if raw is None:
        raise ConversionError("qfq reconstruction requires the same-day BaoStock raw side")
    side = BaoStockDailySide(
        revision.code,
        revision.trade_date,
        "qfq",
        _scaled_price(raw.open_price, factor),
        _scaled_price(raw.high_price, factor),
        _scaled_price(raw.low_price, factor),
        _scaled_price(raw.close_price, factor),
        raw.volume,
        raw.amount,
        None,
        None,
        None,
        raw.trading_status,
    )
    payload = {
        "code": side.code,
        "trade_date": side.trade_date.isoformat(),
        "adjustment": side.adjustment,
        "open_price": side.open_price,
        "high_price": side.high_price,
        "low_price": side.low_price,
        "close_price": side.close_price,
        "volume": side.volume,
        "amount": side.amount,
        "preclose": side.preclose,
        "pct_change": side.pct_change,
        "turnover": side.turnover,
        "trading_status": side.trading_status,
    }
    return _canonical_json(payload)


def _reconstruct_unavailable_qfq(
    revisions_by_code: dict[str, tuple[HistoryMonthlyRevision, ...]],
    unavailable_keys: frozenset[tuple[str, date, str]],
) -> tuple[BaoStockGapRecord, ...]:
    requested_by_code: dict[str, set[date]] = {}
    for code, trade_date, family in unavailable_keys:
        if family != "daily_qfq":
            raise ConversionError("official factor reconstruction is restricted to qfq gaps")
        requested_by_code.setdefault(code, set()).add(trade_date)
    repaired: list[BaoStockGapRecord] = []
    for code, requested_dates in sorted(requested_by_code.items()):
        revisions = revisions_by_code.get(code, ())
        by_date = {item.trade_date: index for index, item in enumerate(revisions)}
        missing_indices: list[int] = []
        for trade_date in sorted(requested_dates):
            index = by_date.get(trade_date)
            if index is None:
                raise ConversionError("qfq gap has no same-day monthly row")
            revision = revisions[index]
            if revision.cell.unadjusted is None or revision.cell.qfq is not None:
                raise ConversionError("qfq gap row is not eligible for official factor reconstruction")
            missing_indices.append(index)
        last_gap = max(missing_indices)
        anchor_index = next(
            (index for index in range(last_gap + 1, len(revisions)) if _is_qfq_anchor(revisions[index])),
            None,
        )
        if anchor_index is None:
            raise ConversionError("qfq gap has no subsequent BaoStock qfq anchor")
        anchor = revisions[anchor_index]
        assert anchor.cell.qfq is not None and anchor.cell.unadjusted is not None
        factor = _side_factor(anchor.cell.qfq, anchor.cell.unadjusted)
        requested_indices = set(missing_indices)
        for index in range(anchor_index - 1, min(missing_indices) - 1, -1):
            current_raw = revisions[index + 1].cell.unadjusted
            previous = revisions[index]
            previous_raw = previous.cell.unadjusted
            if (
                current_raw is None
                or previous_raw is None
                or current_raw.preclose is None
                or previous_raw.close_price is None
                or current_raw.preclose <= 0
                or previous_raw.close_price <= 0
            ):
                raise ConversionError("qfq reconstruction chain lacks a positive BaoStock close or preclose")
            factor = (factor * Decimal(str(current_raw.preclose)) / Decimal(str(previous_raw.close_price))).quantize(
                _QFQ_FACTOR_QUANTUM, rounding=ROUND_HALF_UP
            )
            if previous.cell.qfq is not None:
                factor = _side_factor(previous.cell.qfq, previous_raw)
            if index in requested_indices:
                repaired.append(
                    BaoStockGapRecord(
                        code,
                        previous.trade_date,
                        "daily_qfq",
                        _reconstructed_qfq_payload(previous, factor),
                    )
                )
    if {(item.code, item.trade_date, item.family) for item in repaired} != unavailable_keys:
        raise ConversionError("official qfq reconstruction did not cover every unavailable identity")
    return tuple(sorted(repaired))


def _is_qfq_anchor(revision: HistoryMonthlyRevision) -> bool:
    raw = revision.cell.unadjusted
    qfq = revision.cell.qfq
    return (
        raw is not None
        and qfq is not None
        and raw.close_price is not None
        and qfq.close_price is not None
        and raw.close_price > 0
        and qfq.close_price > 0
    )


def _repaired_qfq_revision(
    sequence: int,
    current: HistoryMonthlyRevision,
    item: BaoStockGapRecord,
) -> HistoryMonthlyRevision:
    qfq_payload = _object(json.loads(item.payload_json), "repaired qfq payload")
    trading_status = qfq_payload["trading_status"]
    if trading_status not in {"trading", "suspended"}:
        raise ConversionError("repaired qfq trading status is invalid")
    qfq = BaoStockDailySide(
        item.code,
        item.trade_date,
        "qfq",
        cast(float | None, qfq_payload["open_price"]),
        cast(float | None, qfq_payload["high_price"]),
        cast(float | None, qfq_payload["low_price"]),
        cast(float | None, qfq_payload["close_price"]),
        cast(float | None, qfq_payload["volume"]),
        cast(float | None, qfq_payload["amount"]),
        None,
        None,
        None,
        trading_status,
    )
    status: Literal["complete", "supplier_marked_suspended"] = (
        "supplier_marked_suspended" if qfq.trading_status == "suspended" else "complete"
    )
    cell = BaoStockDailyCell(item.code, item.trade_date, status, current.cell.unadjusted, qfq)
    return HistoryMonthlyRevision(
        sequence,
        current.board,
        cell,
        current.is_st,
        current.industry,
        current.industry_classification,
    )


def _repair_completed_qfq_gaps(  # noqa: PLR0913
    target: Path,
    archive: SourceArchive,
    batch_size: int,
    cache_mib: int,
    throttle_seconds: float,
    cancellation: _Cancellation,
    progress_sink: ProgressSink | None,
    provider: SupplementProvider,
    fault_injector: FaultInjector,
) -> ConversionSummary:
    control = SQLiteHistoryControlRepository(target / "control.sqlite3")
    state = control.load_state()
    active = state.active_snapshot
    if active is None:
        raise ConversionError("completed archive has no active snapshot")
    _recover_partition_replacements(target, active)
    source_requests = _source_failed_qfq_requests(archive, cache_mib)
    expected_gap_count = next(item[3] for item in archive.field_coverage if item[0] == "daily_qfq")
    if sum(len(item.trade_dates) for item in source_requests) != expected_gap_count:
        raise ConversionError("source qfq failure checkpoints do not match sealed field coverage")
    if not source_requests:
        completed = _read_completed_metadata(
            target,
            archive,
            batch_size,
            cache_mib,
            round(throttle_seconds * 1000),
            remaining_gaps=0,
        )
        if completed is None:
            raise ConversionError("completed archive disappeared during qfq repair")
        return completed
    codes = tuple(sorted({item.code for item in source_requests}))
    first_date = min(day for item in source_requests for day in item.trade_dates)
    revisions_by_code, maximum_sequence = _latest_revisions_for_codes(
        target,
        active,
        codes,
        first_date,
        cache_mib,
    )
    missing_by_code: dict[str, list[date]] = {}
    for request in source_requests:
        current_by_date = {item.trade_date: item for item in revisions_by_code.get(request.code, ())}
        for trade_date in request.trade_dates:
            current = current_by_date.get(trade_date)
            if current is None:
                raise ConversionError("source qfq failure is absent from the completed archive")
            if current.cell.qfq is None:
                missing_by_code.setdefault(request.code, []).append(trade_date)
    requests = tuple(
        BaoStockGapRequest(code, "daily_qfq", tuple(days)) for code, days in sorted(missing_by_code.items())
    )
    if not requests:
        completed = _read_completed_metadata(
            target,
            archive,
            batch_size,
            cache_mib,
            round(throttle_seconds * 1000),
            remaining_gaps=0,
        )
        if completed is None:
            raise ConversionError("completed archive disappeared during qfq repair")
        return completed
    fetched = _fetch_completed_gaps(requests, cancellation, progress_sink, provider)
    unavailable_keys = frozenset((item.code, item.trade_date, item.family) for item in fetched.unavailable)
    reconstructed = _reconstruct_unavailable_qfq(revisions_by_code, unavailable_keys)
    complete_result = BaoStockGapResult(tuple((*fetched.records, *reconstructed)), ())
    requested_keys = {(item.code, day, item.family) for item in requests for day in item.trade_dates}
    if {(item.code, item.trade_date, item.family) for item in complete_result.records} != requested_keys:
        raise ConversionError("completed archive qfq repair is incomplete")

    sequence = maximum_sequence + 1
    repaired_revisions: list[HistoryMonthlyRevision] = []
    current_by_key = {
        (revision.code, revision.trade_date): revision
        for revisions in revisions_by_code.values()
        for revision in revisions
    }
    for item in complete_result.records:
        current = current_by_key.get((item.code, item.trade_date))
        if current is None or current.cell.unadjusted is None or current.cell.qfq is not None:
            raise ConversionError("completed archive qfq repair target changed during preparation")
        try:
            repaired_revisions.append(_repaired_qfq_revision(sequence, current, item))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ConversionError(
                f"repaired qfq payload violates the monthly archive contract: {item.code}:{item.trade_date}"
            ) from exc

    active_by_month = {
        (
            int(PurePosixPath(item.relative_path).parts[1]),
            int(PurePosixPath(item.relative_path).stem),
        ): item
        for item in active.partitions
    }
    affected_months = sorted({(item.trade_date.year, item.trade_date.month) for item in repaired_revisions})
    pending_paths = {
        month: target / "partitions" / f"{month[0]:04d}" / f".{month[1]:02d}.pending.sqlite3"
        for month in affected_months
    }
    pending = _PendingPartitions(
        target, pending_paths, active_by_month, min(item.trade_date for item in repaired_revisions)
    )
    for month, path in pending_paths.items():
        _remove_pending(path)
        reference = active_by_month.get(month)
        if reference is None:
            raise ConversionError("qfq repair month is absent from the active snapshot")
        _backup_database(target / reference.relative_path, path)
    observed_at = datetime.now(_SHANGHAI)
    old_source = next(item for item in state.sources if item.content_hash == active.source_identity_hash)
    old_calendar = next(item for item in state.calendars if item.content_hash == active.calendar_hash)
    old_universe = next(item for item in state.universes if item.content_hash == active.universe_hash)
    source = HistorySourceIdentity(
        "baostock",
        old_source.dataset,
        "sealed_parent_increment_official_qfq_repair",
        observed_at,
    )
    calendar = HistoryCalendarIdentity(old_calendar.open_dates, source.content_hash)
    universe = HistoryUniverseIdentity(old_universe.securities, source.content_hash)
    sealed = None
    try:
        _write_revisions(pending, tuple(repaired_revisions))
        fault_injector("completed_qfq_repair_prepared")
        sealed = _seal_pending(target, pending)
        replacements_by_month = {
            (
                int(PurePosixPath(item.relative_path).parts[1]),
                int(PurePosixPath(item.relative_path).stem),
            ): item
            for item in sealed.references
        }
        references = tuple(replacements_by_month.get(month, reference) for month, reference in active_by_month.items())
        snapshot = HistoryActiveSnapshot(
            sequence,
            active.data_cutoff,
            active.label_cutoff,
            calendar.content_hash,
            universe.content_hash,
            source.content_hash,
            references,
        )
        fault_injector("completed_qfq_partitions_replaced")
        _publish_snapshot(
            control,
            (source, calendar, universe),
            snapshot,
            HistorySyncCheckpoint(
                f"conversion_qfq_repair.{snapshot.content_hash[:24]}",
                sequence,
                "completed",
                observed_at,
                len(repaired_revisions),
                len(repaired_revisions),
                None,
            ),
        )
        fault_injector("completed_qfq_snapshot_published")
    except BaseException:
        if sealed is not None:
            published = control.load_state().active_snapshot
            if published is not None and "snapshot" in locals() and published.content_hash == snapshot.content_hash:
                _discard_partition_replacements(sealed.replacements)
            else:
                _restore_partition_replacements(sealed.replacements)
        raise
    else:
        _discard_partition_replacements(sealed.replacements)
    finally:
        _remove_sync_pending(pending)
        for reference in (active_by_month[month] for month in affected_months):
            _remove_sqlite_sidecars(target / reference.relative_path)
    completed = _read_completed_metadata(
        target,
        archive,
        batch_size,
        cache_mib,
        round(throttle_seconds * 1000),
        remaining_gaps=0,
    )
    if completed is None or completed.remaining_downloadable_gaps:
        raise ConversionError("published qfq repair did not produce a complete archive")
    return replace(completed, state="supplemented", supplemented_rows=len(repaired_revisions))


def _read_hash_layout_snapshot(target: Path, source_fingerprint: str, cache_mib: int) -> _HashLayoutSnapshot | None:
    control_path = target / "control.sqlite3"
    if not control_path.is_file():
        return None
    try:
        with closing(_connect(control_path, cache_mib, read_only=True)) as connection:
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise ConversionError("hash-layout control database failed integrity check")
            if connection.execute("SELECT schema_identity FROM metadata WHERE singleton=1").fetchone() != (
                "history_control",
            ):
                return None
            active = connection.execute(
                "SELECT snapshot_hash, sequence FROM active_snapshot WHERE singleton=1"
            ).fetchone()
            snapshot_rows = connection.execute(
                "SELECT record_key, content_hash, payload_json FROM immutable_records "
                "WHERE kind='snapshot' ORDER BY record_key"
            ).fetchall()
            if active is None or len(snapshot_rows) != 1:
                return None
            record_key, stored_hash, payload_json = cast(tuple[str, str, str], snapshot_rows[0])
            payload = _object(json.loads(payload_json), "hash-layout snapshot")
            required = {
                "sequence",
                "data_cutoff",
                "label_cutoff",
                "calendar_hash",
                "universe_hash",
                "source_identity_hash",
                "partitions",
            }
            if set(payload) != required:
                raise ConversionError("hash-layout snapshot fields are invalid")
            sequence = _integer(payload["sequence"], "hash-layout snapshot sequence")
            if record_key != str(sequence) or active != (stored_hash, sequence):
                raise ConversionError("hash-layout active snapshot pointer is inconsistent")
            if _hash_text(_canonical_json(payload)) != stored_hash:
                raise ConversionError("hash-layout snapshot hash is invalid")
            raw_partitions = _list(payload["partitions"], "hash-layout partitions")
            old_layout_flags: list[bool] = []
            parsed: list[_HashLayoutPartition] = []
            for item in raw_partitions:
                raw = _object(item, "hash-layout partition")
                if set(raw) != {"relative_path", "sha256", "row_count"}:
                    raise ConversionError("hash-layout partition fields are invalid")
                relative_path = _string(raw["relative_path"], "hash-layout partition path")
                sha256 = _string(raw["sha256"], "hash-layout partition SHA-256")
                row_count = _integer(raw["row_count"], "hash-layout partition row count")
                parts = PurePosixPath(relative_path).parts
                is_old_layout = (
                    len(parts) == 4
                    and parts[0] == "partitions"
                    and len(parts[1]) == 4
                    and parts[1].isdigit()
                    and len(parts[2]) == 2
                    and parts[2].isdigit()
                    and 1 <= int(parts[2]) <= 12
                    and PurePosixPath(parts[3]).suffix == ".sqlite3"
                    and PurePosixPath(parts[3]).stem == sha256
                )
                old_layout_flags.append(is_old_layout)
                if is_old_layout:
                    current = HistorySnapshotPartition(
                        f"partitions/{parts[1]}/{parts[2]}.sqlite3",
                        sha256,
                        row_count,
                    )
                    parsed.append(_HashLayoutPartition(relative_path, current))
            if not old_layout_flags or not any(old_layout_flags):
                return None
            if not all(old_layout_flags):
                raise ConversionError("hash-layout snapshot mixes incompatible partition paths")
            source_identity_hash = _string(payload["source_identity_hash"], "hash-layout source identity")
            source_row = connection.execute(
                "SELECT payload_json FROM immutable_records WHERE kind='source' AND content_hash=?",
                (source_identity_hash,),
            ).fetchone()
            if source_row is None:
                raise ConversionError("hash-layout source identity is missing")
            source_payload = _object(json.loads(cast(str, source_row[0])), "hash-layout source identity")
            if source_payload.get("dataset") != f"legacy_daily.{source_fingerprint}":
                raise ConversionError("target already exists for a different source archive")
        current_snapshot = HistoryActiveSnapshot(
            sequence,
            date.fromisoformat(_string(payload["data_cutoff"], "hash-layout data cutoff")),
            date.fromisoformat(_string(payload["label_cutoff"], "hash-layout label cutoff")),
            _string(payload["calendar_hash"], "hash-layout calendar hash"),
            _string(payload["universe_hash"], "hash-layout universe hash"),
            source_identity_hash,
            tuple(item.current_reference for item in parsed),
        )
    except ConversionError:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        raise ConversionError("hash-layout target control database is invalid") from exc
    return _HashLayoutSnapshot(stored_hash, current_snapshot, tuple(parsed))


def _current_snapshot_payload(snapshot: HistoryActiveSnapshot) -> dict[str, object]:
    return {
        "sequence": snapshot.sequence,
        "data_cutoff": snapshot.data_cutoff.isoformat(),
        "label_cutoff": snapshot.label_cutoff.isoformat(),
        "calendar_hash": snapshot.calendar_hash,
        "universe_hash": snapshot.universe_hash,
        "source_identity_hash": snapshot.source_identity_hash,
        "partitions": [
            {
                "relative_path": item.relative_path,
                "sha256": item.sha256,
                "row_count": item.row_count,
            }
            for item in snapshot.partitions
        ],
    }


def _prepare_current_layout_control(
    target: Path,
    layout: _HashLayoutSnapshot,
    cache_mib: int,
) -> Path:
    control_path = target / "control.sqlite3"
    pending = target / ".control.layout.pending.sqlite3"
    _remove_pending(pending)
    try:
        with closing(sqlite3.connect(control_path)) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise ConversionError("hash-layout control WAL checkpoint did not complete")
        _remove_empty_sqlite_sidecars(control_path)
        with (
            closing(_connect(control_path, cache_mib, read_only=True)) as source_connection,
            closing(sqlite3.connect(pending)) as pending_connection,
        ):
            source_connection.backup(pending_connection)
        with closing(_connect(pending, cache_mib)) as connection, connection:
            updated = connection.execute(
                "UPDATE immutable_records SET content_hash=?, payload_json=? "
                "WHERE kind='snapshot' AND record_key=? AND content_hash=?",
                (
                    layout.current.content_hash,
                    _canonical_json(_current_snapshot_payload(layout.current)),
                    str(layout.current.sequence),
                    layout.previous_hash,
                ),
            ).rowcount
            pointer_updated = connection.execute(
                "UPDATE active_snapshot SET snapshot_hash=? WHERE singleton=1 AND snapshot_hash=? AND sequence=?",
                (layout.current.content_hash, layout.previous_hash, layout.current.sequence),
            ).rowcount
            if updated != 1 or pointer_updated != 1:
                raise ConversionError("hash-layout control update did not match its active snapshot")
        state = SQLiteHistoryControlRepository(pending).load_state()
        if state.active_snapshot != layout.current:
            raise ConversionError("current-layout control database did not round-trip")
        with closing(sqlite3.connect(pending)) as connection:
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint is None or checkpoint[0] != 0:
                raise ConversionError("current-layout control WAL checkpoint did not complete")
        _remove_empty_sqlite_sidecars(pending)
        _fsync_file(pending)
        return pending
    except ConversionError:
        raise
    except (HistoryControlError, OSError, sqlite3.DatabaseError) as exc:
        raise ConversionError("current-layout control database preparation failed") from exc


def _normalize_hash_layout_target(
    target: Path,
    source_fingerprint: str,
    cache_mib: int,
    progress_sink: ProgressSink | None,
    fault_injector: FaultInjector,
) -> bool:
    layout = _read_hash_layout_snapshot(target, source_fingerprint, cache_mib)
    if layout is None:
        return False
    pending = _prepare_current_layout_control(target, layout, cache_mib)
    fault_injector("hash_layout_control_prepared")
    progress = _ProgressTracker("布局归一化", len(layout.partitions), progress_sink)
    previous_directories: set[Path] = set()
    for item in layout.partitions:
        previous = _safe_child(target, item.previous_relative_path, "hash-layout partition")
        current = _safe_child(target, item.current_reference.relative_path, "current-layout partition")
        previous_directories.add(previous.parent)
        if previous.is_file() and current.exists():
            raise ConversionError("hash-layout partition has duplicate old and current files")
        candidate = previous if previous.is_file() else current
        if not candidate.is_file():
            raise ConversionError("hash-layout partition is missing from both resumable locations")
        try:
            SQLiteHistoryMonthPartitionRepository.verify(candidate, item.current_reference)
        except HistoryMonthPartitionError as exc:
            raise ConversionError("hash-layout partition verification failed") from exc
        _remove_empty_sqlite_sidecars(candidate)
        if candidate == previous:
            current.parent.mkdir(parents=True, exist_ok=True)
            os.replace(previous, current)
            _fsync_directory(current.parent)
            _fsync_directory(previous.parent)
        progress.advance(1, item.current_reference.relative_path, force=True)
    fault_injector("hash_layout_partitions_renamed")
    control_path = target / "control.sqlite3"
    _remove_empty_sqlite_sidecars(control_path)
    os.replace(pending, control_path)
    _fsync_directory(target)
    fault_injector("hash_layout_control_published")
    for directory in sorted(previous_directories, key=lambda value: len(value.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue
    return True


def _source_size(source: SourceArchive) -> int:
    paths = {item.path for item in source.parent_partitions}
    paths.update(item.path for item in source.increment_partitions)
    return sum(path.stat().st_size for path in paths)


def _check_free_space(target: Path, required: int) -> None:
    probe = target.parent
    while not probe.exists():
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < required:
        raise ConversionError(f"insufficient free disk space: require {required} bytes, available {free} bytes")


def _validate_staging_directory(staging: Path, progress_database: Path, *, publishing: bool = False) -> None:
    if not staging.exists():
        if publishing:
            raise ConversionError("conversion staging directory disappeared")
        return
    sealed_control = staging / "control.sqlite3"
    resumable = progress_database.is_file() or sealed_control.is_file()
    if not staging.is_dir() or (not publishing and not resumable):
        raise ConversionError("staging directory is not a resumable conversion")
    allowed = {
        progress_database.name,
        f"{progress_database.name}-journal",
        f"{progress_database.name}-shm",
        f"{progress_database.name}-wal",
        "control.sqlite3",
        "control.sqlite3.pending",
        "control.sqlite3.pending-shm",
        "control.sqlite3.pending-wal",
        "partitions",
    }
    unexpected = {item.name for item in staging.iterdir()} - allowed
    if unexpected:
        raise ConversionError("staging directory contains unrelated files")


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _remove_empty_sqlite_sidecars(path: Path) -> None:
    wal = Path(f"{path}-wal")
    shm = Path(f"{path}-shm")
    try:
        if wal.stat().st_size > 0:
            return
    except FileNotFoundError:
        pass
    for candidate in (wal, shm):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _remove_sqlite_sidecars(path: Path) -> None:
    for candidate in (Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")):
        candidate.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _apply_niceness() -> None:
    if os.name == "posix":
        try:
            os.nice(DEFAULT_NICE_INCREMENT)
        except OSError as exc:
            raise ConversionError("could not lower converter CPU scheduling priority") from exc


def convert_archive(
    source_root: Path,
    target_root: Path,
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    cache_mib: int = DEFAULT_CACHE_MIB,
    throttle_seconds: float = DEFAULT_THROTTLE_MS / 1000,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_MIB * 1024 * 1024,
    apply_niceness: bool = True,
    progress_sink: ProgressSink | None = None,
    supplement_missing: bool = False,
    supplement_provider: SupplementProvider | None = None,
    fault_injector: FaultInjector | None = None,
) -> ConversionSummary:
    """Convert a sealed archive and normalize this converter's completed hash layout."""

    if batch_size < 1 or batch_size > 4096:
        raise ConversionError("batch size must be within 1..4096")
    if cache_mib < 1 or cache_mib > 64:
        raise ConversionError("cache MiB must be within 1..64")
    if throttle_seconds < 0 or throttle_seconds > 1:
        raise ConversionError("throttle must be within 0..1 seconds")
    if minimum_free_bytes < 0:
        raise ConversionError("minimum free bytes cannot be negative")
    source = source_root.resolve()
    target = target_root.resolve()
    if not source.is_dir():
        raise ConversionError("source archive directory is missing")
    if source == target or source in target.parents or target in source.parents:
        raise ConversionError("source and target must be separate sibling trees")
    if apply_niceness:
        _apply_niceness()
    inject_fault = fault_injector or (lambda _stage: None)
    with _SourceLock(source / ".download.lock"), _Cancellation() as cancellation:
        archive = _load_source(source)
        if supplement_missing:
            downloadable_gaps = sum(
                item[3] for item in archive.field_coverage if item[0] in DOWNLOADABLE_FIELD_FAMILIES
            )
            try:
                completed_metadata = _read_completed_metadata(
                    target,
                    archive,
                    batch_size,
                    cache_mib,
                    round(throttle_seconds * 1000),
                    remaining_gaps=downloadable_gaps,
                )
            except ConversionError:
                completed_metadata = None
            if completed_metadata is not None:
                try:
                    with HistoryMaintenanceLock(target / ".maintenance.lock"):
                        return _repair_completed_qfq_gaps(
                            target,
                            archive,
                            batch_size,
                            cache_mib,
                            throttle_seconds,
                            cancellation,
                            progress_sink,
                            supplement_provider or fetch_baostock_gaps,
                            inject_fault,
                        )
                except HistoryMaintenanceAlreadyRunningError as exc:
                    raise ConversionError("history maintenance is already running") from exc
        try:
            completed = _read_completed_summary(
                target,
                archive.source_fingerprint,
                batch_size,
                cache_mib,
                round(throttle_seconds * 1000),
                progress_sink,
            )
        except ConversionError as current_error:
            try:
                with HistoryMaintenanceLock(target / ".maintenance.lock"):
                    normalized = _normalize_hash_layout_target(
                        target,
                        archive.source_fingerprint,
                        cache_mib,
                        progress_sink,
                        inject_fault,
                    )
            except HistoryMaintenanceAlreadyRunningError as exc:
                raise ConversionError("history maintenance is already running") from exc
            if not normalized:
                raise current_error
            completed = _read_completed_summary(
                target,
                archive.source_fingerprint,
                batch_size,
                cache_mib,
                round(throttle_seconds * 1000),
                progress_sink,
            )
            if completed is None:
                raise ConversionError("normalized target is not a completed conversion") from None
            completed = replace(completed, state="completed")
        if completed is not None:
            if supplement_missing and completed.remaining_downloadable_gaps:
                try:
                    with HistoryMaintenanceLock(target / ".maintenance.lock"):
                        return _repair_completed_qfq_gaps(
                            target,
                            archive,
                            batch_size,
                            cache_mib,
                            throttle_seconds,
                            cancellation,
                            progress_sink,
                            supplement_provider or fetch_baostock_gaps,
                            inject_fault,
                        )
                except HistoryMaintenanceAlreadyRunningError as exc:
                    raise ConversionError("history maintenance is already running") from exc
            return completed
        if target.exists():
            raise ConversionError("target already exists and is not a completed conversion")
        staging = target.with_name(f".{target.name}-conversion")
        progress_database = staging / ".conversion-progress.sqlite3"
        _validate_staging_directory(staging, progress_database)
        if staging.is_dir() and not progress_database.is_file():
            sealed = _read_completed_summary(
                staging,
                archive.source_fingerprint,
                batch_size,
                cache_mib,
                round(throttle_seconds * 1000),
                progress_sink,
            )
            if sealed is None:
                raise ConversionError("sealed staging conversion is invalid")
            _validate_staging_directory(staging, progress_database, publishing=True)
            os.replace(staging, target)
            _fsync_directory(target.parent)
            return replace(sealed, state="completed")
        staging.mkdir(parents=True, exist_ok=True)
        control = _create_progress_database(progress_database, archive, cache_mib)
        try:
            _check_free_space(target, _source_size(archive) + minimum_free_bytes)
            _verify_source(archive, control, cache_mib, throttle_seconds, cancellation, progress_sink)
            increment_dates = _increment_dates(archive, cache_mib)
            all_dates = tuple(sorted(set(archive.calendar_dates) | increment_dates))
            if all_dates[-1] != archive.source_cutoff:
                raise ConversionError("source cutoff is absent from daily raw/qfq records")
            active_dates = all_dates[-archive.sessions :]
            active_start = active_dates[0]
            months = _months(active_dates)
            board_by_code = {item.code: item.board for item in archive.securities}
            industries = _load_industries(archive, cache_mib)
            total_source_rows = _source_rows_from(archive, active_start, cache_mib)
            progress = _ProgressTracker("转换", total_source_rows, progress_sink)
            results: list[PartitionResult] = []
            for year, month in months:
                cancellation.check()
                existing = _completed_partition(control, staging, year, month, throttle_seconds)
                if existing is not None:
                    results.append(existing)
                    progress.advance(existing.source_rows, f"月份={year:04d}-{month:02d}", force=True)
                    continue
                result = _convert_month(
                    archive,
                    staging,
                    year,
                    month,
                    active_start,
                    board_by_code,
                    industries,
                    batch_size,
                    cache_mib,
                    throttle_seconds,
                    cancellation,
                    progress,
                )
                _save_progress(control, result)
                results.append(result)
                progress.emit(f"月份={year:04d}-{month:02d}", force=True)
            if supplement_missing:
                refreshed, supplemented_rows, remaining_gaps = _supplement_missing_fields(
                    staging,
                    results,
                    board_by_code,
                    industries,
                    cache_mib,
                    throttle_seconds,
                    cancellation,
                    progress_sink,
                    supplement_provider or fetch_baostock_gaps,
                )
                results = list(refreshed)
                for result in results:
                    _save_progress(control, result)
            else:
                gap_requests = _missing_gap_requests(staging, results, cache_mib, progress_sink)
                supplemented_rows = 0
                remaining_gaps = sum(len(item.trade_dates) for item in gap_requests)
            snapshot_hash, active_dates = _finalize_control(
                staging,
                archive,
                all_dates,
                results,
                3 if supplemented_rows else 2,
            )
            physical_rows = sum(item.physical_rows for item in results)
            active_rows = sum(item.active_rows for item in results)
        finally:
            control.close()
        _remove_pending(progress_database)
        _validate_staging_directory(staging, progress_database, publishing=True)
        _fsync_file(staging / "control.sqlite3")
        _fsync_directory(staging)
        os.replace(staging, target)
        _fsync_directory(target.parent)
        return ConversionSummary(
            "completed",
            len(results),
            physical_rows,
            active_rows,
            len(active_dates),
            archive.source_cutoff,
            snapshot_hash,
            batch_size,
            cache_mib,
            round(throttle_seconds * 1000),
            supplemented_rows,
            remaining_gaps,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "把旧 BaoStock 父/增量归档流式转换为 control.sqlite3 + YYYY/MM.sqlite3；"
            "转换全程单进程、逐月旁路构建、原子发布，并可续传已校验月份；"
            "已完成的 hash 子目录结果会在完整校验后原地归一化；"
            "缺失 raw/qfq/is_st 默认由一个受控 SDK 子进程补齐。"
        )
    )
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="旧 sessions-2000 归档目录")
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET, help="新月分片归档目录")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"每批读取行数（默认 {DEFAULT_BATCH_SIZE}，越小内存和瞬时 CPU 越低）",
    )
    parser.add_argument(
        "--cache-mib",
        type=int,
        default=DEFAULT_CACHE_MIB,
        help=f"每个 SQLite 连接的缓存上限 MiB（默认 {DEFAULT_CACHE_MIB}）",
    )
    parser.add_argument(
        "--throttle-ms",
        type=int,
        default=DEFAULT_THROTTLE_MS,
        help=f"每批及每个哈希块后的让步毫秒数（默认 {DEFAULT_THROTTLE_MS}）",
    )
    parser.add_argument(
        "--minimum-free-mib",
        type=int,
        default=DEFAULT_MINIMUM_FREE_MIB,
        help=f"除源分片总大小外还必须保留的磁盘余量 MiB（默认 {DEFAULT_MINIMUM_FREE_MIB}）",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="只转换现有父/增量归档，不联网补 raw/qfq/is_st 缺口",
    )
    parser.add_argument("--no-nice", action="store_true", help="不降低 POSIX CPU 调度优先级")
    return parser


def _summary_payload(value: ConversionSummary) -> dict[str, object]:
    return {
        "active_calendar_days": value.active_calendar_days,
        "active_rows": value.active_rows,
        "batch_size": value.batch_size,
        "cache_mib": value.cache_mib,
        "data_cutoff": value.data_cutoff,
        "partition_count": value.partition_count,
        "physical_rows": value.physical_rows,
        "snapshot_hash": value.snapshot_hash,
        "state": value.state,
        "supplemented_rows": value.supplemented_rows,
        "throttle_ms": value.throttle_ms,
        "remaining_downloadable_gaps": value.remaining_downloadable_gaps,
    }


def _duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds_part:02d}"


def _print_progress(value: ConversionProgress) -> None:
    print(
        f"阶段={value.phase} {value.current} 进度={value.percentage:.2f}% "
        f"已处理={value.completed}/{value.total} 耗时={_duration(value.elapsed_seconds)}",
        file=sys.stderr,
        flush=True,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.throttle_ms < 0 or args.minimum_free_mib < 0:
            raise ConversionError("resource limit arguments cannot be negative")
        summary = convert_archive(
            args.source,
            args.target,
            batch_size=args.batch_size,
            cache_mib=args.cache_mib,
            throttle_seconds=args.throttle_ms / 1000,
            minimum_free_bytes=args.minimum_free_mib * 1024 * 1024,
            apply_niceness=not args.no_nice,
            progress_sink=_print_progress,
            supplement_missing=not args.offline,
        )
    except ConversionError as exc:
        print(_canonical_json({"reason": str(exc), "state": "failed"}), file=sys.stderr)
        return 1
    print(json.dumps(_summary_payload(summary), ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
