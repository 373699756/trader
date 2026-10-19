"""Explicit offline repack of annual price partitions."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from trader.download.domain.history_control import (
    HistoryActiveSnapshot,
    HistoryControlState,
    HistorySnapshotPartition,
)
from trader.download.infra.history_control_index import (
    HistoryControlError,
    HistoryControlIndex,
    HistoryMaintenanceLock,
)
from trader.download.infra.history_partition_repack_codec import (
    HistoryPartitionRepackCodecError,
    ReadHistoryPartitionRepackActivationJournal,
    ReadHistoryPartitionRepackBuildState,
    ReadTomorrowTrainingMemoryEvidence,
    WriteHistoryPartitionRepackActivationJournal,
    WriteHistoryPartitionRepackBuildState,
)
from trader.download.infra.history_partition_repack_state import (
    HistoryPartitionRepackAction,
    HistoryPartitionRepackActivationJournal,
    HistoryPartitionRepackBuildState,
    HistoryPartitionRepackPartitionEvidence,
    HistoryPartitionRepackRequirements,
    HistoryPartitionRepackSourceFileIdentity,
    HistoryPartitionRepackStatus,
)
from trader.download.infra.history_prices import HistoryPricePartition
from trader.download.infra.price_columns import PRICE_COLUMNS
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.infra.sqlite_connection import OpenSqliteConnection, SqliteConnection, SqliteConnectionOptions
from trader.training.infra.model_bundles.profile_publication import InspectActiveProfileBundles

RepackProgress = Callable[[int, int, str], None]
FaultInjector = Callable[[str], None]
_BUILD_STATE_NAME = "history-repack-state.json"
_ACTIVATION_JOURNAL_NAME = "history-repack-activation.json"
_CONTROL_NAME = "catalog.sqlite3"
_HASH_CHUNK_BYTES = 1024 * 1024
_TABLE_QUERIES = (
    (
        "daily_records",
        "SELECT trade_date, code, revision_id, first_seen_sequence, board, "
        + ", ".join(PRICE_COLUMNS)
        + ", content_hash FROM daily_records ORDER BY trade_date, code, revision_id",
    ),
    (
        "daily_observations",
        "SELECT trade_date, code, sync_sequence, revision_id FROM daily_observations "
        "ORDER BY trade_date, code, sync_sequence",
    ),
)


class HistoryPartitionRepackError(RuntimeError):
    """The physical archive repack cannot safely continue."""


class HistoryPartitionRepackFenceError(HistoryPartitionRepackError):
    """Normal archive work is fenced by an unfinished activation."""


@dataclass(frozen=True)
class _RepackLayout:
    source_root: Path
    target_root: Path
    backup_root: Path
    state_path: Path
    journal_path: Path


@dataclass(frozen=True)
class _SourceContext:
    control_state: HistoryControlState
    active: HistoryActiveSnapshot
    source_files: tuple[HistoryPartitionRepackSourceFileIdentity, ...]
    source_file_identity_hash: str
    source_bytes: int
    security_count: int
    trading_day_count: int


@dataclass(frozen=True)
class _PartitionFacts:
    row_count: int
    observation_count: int
    latest_row_count: int
    records_hash: str
    observations_hash: str


class HistoryPartitionRepackCoordinator:
    """Build an isolated copy; switching and finalization require explicit calls."""

    def __init__(
        self,
        source_root: Path,
        target_root: Path,
        *,
        requirements: HistoryPartitionRepackRequirements | None = None,
        progress: RepackProgress | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        source, target = source_root.expanduser().resolve(), target_root.expanduser().resolve()
        if source.name != "history" or target != source.with_name("history-repacked"):
            raise HistoryPartitionRepackError("repack requires sibling history and history-repacked directories")
        self._layout = _RepackLayout(
            source,
            target,
            source.with_name("history-before-repack"),
            source.with_name(_BUILD_STATE_NAME),
            source.with_name(_ACTIVATION_JOURNAL_NAME),
        )
        self._requirements = requirements or HistoryPartitionRepackRequirements()
        self._progress = progress
        self._inject = fault_injector or (lambda _stage: None)

    def build(self) -> HistoryPartitionRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root.with_name(".history.lock")):
            _require_no_activation_fence(layout.journal_path)
            context = _load_source_context(layout.source_root, self._requirements)
            _require_same_filesystem(layout)
            if layout.state_path.exists():
                state = ReadHistoryPartitionRepackBuildState(layout.state_path)
                _require_state_source(state, layout, context, self._requirements)
            else:
                if layout.target_root.exists() and any(layout.target_root.iterdir()):
                    raise HistoryPartitionRepackError("repack target is nonempty without trusted state")
                _require_free_space(layout, context, self._requirements)
                layout.target_root.mkdir(parents=True, exist_ok=True)
                state = HistoryPartitionRepackBuildState(
                    str(layout.source_root),
                    str(layout.target_root),
                    context.active.content_hash,
                    context.active.sequence,
                    context.source_file_identity_hash,
                    context.source_files,
                    context.source_bytes,
                    context.security_count,
                    context.trading_day_count,
                    self._requirements.expected_partition_count,
                    self._requirements.target_page_size,
                    (),
                )
                WriteHistoryPartitionRepackBuildState(layout.state_path, state)
                self._inject("build_prepared")
            if state.completed:
                _verify_candidate(layout.target_root, state, target=True)
                return _status("build", "completed", state)
            completed = {item.relative_path: item for item in state.partitions}
            for reference in context.active.partitions:
                evidence = completed.get(reference.relative_path)
                if evidence is None:
                    evidence = _repack_partition(
                        layout.source_root, layout.target_root, reference, context.active.sequence, state.page_size
                    )
                    self._inject(f"partition_replaced:{reference.relative_path}")
                    completed[reference.relative_path] = evidence
                    state = replace(
                        state, partitions=tuple(sorted(completed.values(), key=lambda item: item.relative_path))
                    )
                    WriteHistoryPartitionRepackBuildState(layout.state_path, state)
                    if self._progress:
                        self._progress(len(state.partitions), state.expected_partition_count, reference.relative_path)
                elif _sha256_file(layout.target_root / reference.relative_path) != evidence.target_sha256:
                    raise HistoryPartitionRepackError("resumed annual file differs from committed digest")
                HistoryPricePartition.verify(layout.target_root / reference.relative_path, reference)
            _require_source_unchanged(layout.source_root, state)
            _require_size_gate(state, self._requirements)
            target_control = layout.target_root / _CONTROL_NAME
            shutil.copyfile(layout.source_root / _CONTROL_NAME, target_control)
            _fsync_file(target_control)
            _fsync_directory(layout.target_root)
            # Physical compaction changes no logical snapshot or reference identity.
            state = replace(state, target_snapshot_hash=context.active.content_hash, completed=True)
            WriteHistoryPartitionRepackBuildState(layout.state_path, state)
            _verify_candidate(layout.target_root, state, target=True)
            self._inject("build_completed")
            return _status("build", "completed", state)

    def activate(self) -> HistoryPartitionRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root.with_name(".history.lock")):
            state = self._completed_state()
            journal = _optional_journal(layout.journal_path)
            if journal is not None:
                _require_journal_layout(journal, layout, state)
                if journal.state == "finalized":
                    return _status("activate", "finalized", state)
                if journal.state == "finalizing":
                    raise HistoryPartitionRepackError("resume finalization before another repack operation")
                if journal.state != "rolled_back":
                    if layout.source_root.exists() and layout.backup_root.exists() and not layout.target_root.exists():
                        _verify_candidate(layout.source_root, state, target=True)
                        WriteHistoryPartitionRepackActivationJournal(
                            layout.journal_path, replace(journal, state="verified")
                        )
                        return _status("activate", "verified", state)
                    self._restore(journal, state)
                    return _status("activate", "rolled_back", state)
            if layout.backup_root.exists():
                raise HistoryPartitionRepackError("repack backup already exists")
            _verify_candidate(layout.source_root, state, target=False)
            _verify_candidate(layout.target_root, state, target=True)
            journal = HistoryPartitionRepackActivationJournal(
                "prepared",
                str(layout.source_root),
                str(layout.target_root),
                str(layout.backup_root),
                state.source_snapshot_hash,
                cast(str, state.target_snapshot_hash),
            )
            WriteHistoryPartitionRepackActivationJournal(layout.journal_path, journal)
            self._inject("prepared")
            _move_path(layout.source_root, layout.backup_root)
            self._inject("old_root_moved")
            journal = replace(journal, state="old_root_moved")
            WriteHistoryPartitionRepackActivationJournal(layout.journal_path, journal)
            _move_path(layout.target_root, layout.source_root)
            self._inject("new_root_activated")
            _verify_candidate(layout.source_root, state, target=True)
            WriteHistoryPartitionRepackActivationJournal(layout.journal_path, replace(journal, state="verified"))
            self._inject("verified")
            return _status("activate", "verified", state)

    def rollback(self) -> HistoryPartitionRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root.with_name(".history.lock")):
            state = self._completed_state()
            journal = ReadHistoryPartitionRepackActivationJournal(layout.journal_path)
            _require_journal_layout(journal, layout, state)
            if journal.state in {"finalizing", "finalized"}:
                raise HistoryPartitionRepackError("finalized repack cannot be rolled back")
            self._restore(journal, state)
            return _status("rollback", "rolled_back", state)

    def _restore(
        self, journal: HistoryPartitionRepackActivationJournal, state: HistoryPartitionRepackBuildState
    ) -> None:
        layout = self._layout
        if layout.backup_root.exists():
            _verify_candidate(layout.backup_root, state, target=False)
            if layout.source_root.exists():
                _verify_candidate(layout.source_root, state, target=True)
                if layout.target_root.exists():
                    raise HistoryPartitionRepackError("rollback target already exists")
                _move_path(layout.source_root, layout.target_root)
            _move_path(layout.backup_root, layout.source_root)
        _verify_candidate(layout.source_root, state, target=False)
        WriteHistoryPartitionRepackActivationJournal(layout.journal_path, replace(journal, state="rolled_back"))
        self._inject("rolled_back")

    def finalize(self, training_root: Path, memory_evidence_path: Path) -> HistoryPartitionRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root.with_name(".history.lock")):
            state = self._completed_state()
            journal = ReadHistoryPartitionRepackActivationJournal(layout.journal_path)
            _require_journal_layout(journal, layout, state)
            if journal.state == "finalized":
                return _status("finalize", "finalized", state)
            if journal.state not in {"verified", "finalizing"}:
                raise HistoryPartitionRepackError("repack must be verified before finalization")
            _verify_candidate(layout.source_root, state, target=True)
            from trader.training.infra.profile.v3.contracts import V3_TRAINING_PROFILE

            bundle = InspectActiveProfileBundles(training_root.resolve() / "v3", V3_TRAINING_PROFILE)[0]
            evidence = ReadTomorrowTrainingMemoryEvidence(memory_evidence_path.resolve())
            if (
                bundle.training_input_hash != state.target_snapshot_hash
                or evidence.training_input_hash != state.target_snapshot_hash
                or evidence.model_hash != bundle.model_hash
                or evidence.report_hash != bundle.report_hash
            ):
                raise HistoryPartitionRepackError("training evidence does not match repacked prices")
            if journal.state == "verified":
                _verify_candidate(layout.backup_root, state, target=False)
                journal = replace(journal, state="finalizing")
                WriteHistoryPartitionRepackActivationJournal(layout.journal_path, journal)
            released = _directory_size(layout.backup_root) if layout.backup_root.exists() else 0
            if layout.backup_root.exists():
                shutil.rmtree(layout.backup_root)
            _fsync_directory(layout.backup_root.parent)
            WriteHistoryPartitionRepackActivationJournal(layout.journal_path, replace(journal, state="finalized"))
            return replace(_status("finalize", "finalized", state), released_bytes=released)

    def _completed_state(self) -> HistoryPartitionRepackBuildState:
        state = ReadHistoryPartitionRepackBuildState(self._layout.state_path)
        if (
            not state.completed
            or state.source_root != str(self._layout.source_root)
            or state.target_root != str(self._layout.target_root)
        ):
            raise HistoryPartitionRepackError("repack completed state is invalid")
        return state


def RequireHistoryPartitionRepackInactive(history_root: Path) -> None:
    _require_no_activation_fence(history_root.resolve().with_name(_ACTIVATION_JOURNAL_NAME))


def _verify_candidate(root: Path, state: HistoryPartitionRepackBuildState, *, target: bool) -> None:
    _require_clean_source(root)
    _require_stable_snapshot(root, state.source_snapshot_hash)
    active = HistoryControlIndex(root / _CONTROL_NAME).load_state().active_snapshot
    if active is None:
        raise HistoryPartitionRepackError("repack snapshot missing")
    _clear_empty_partition_sidecars({root / _CONTROL_NAME, *(root / item.relative_path for item in active.partitions)})
    expected = {_CONTROL_NAME, *(item.relative_path for item in state.partitions)}
    if not root.is_dir() or root.is_symlink() or {path.name for path in root.iterdir()} != expected:
        raise HistoryPartitionRepackError("repack directory has missing or unexpected files")
    for item in state.partitions:
        path = root / item.relative_path
        if path.is_symlink() or _sha256_file(path) != (item.target_sha256 if target else item.source_sha256):
            raise HistoryPartitionRepackError("repack annual file digest mismatch")
    for reference in active.partitions:
        HistoryPricePartition.verify(root / reference.relative_path, reference)


def _load_source_context(root: Path, requirements: HistoryPartitionRepackRequirements) -> _SourceContext:
    if not root.is_dir() or root.is_symlink():
        raise HistoryPartitionRepackError("history repack source archive is missing or unsafe")
    _require_clean_source(root)
    control = HistoryControlIndex(root / _CONTROL_NAME)
    try:
        if control.integrity().state != "healthy":
            raise HistoryPartitionRepackError("history repack source control is unavailable")
        state = control.load_state()
    except HistoryControlError as exc:
        raise HistoryPartitionRepackError("history repack source control is unavailable") from exc
    active = state.active_snapshot
    if active is None:
        raise HistoryPartitionRepackError("history repack source has no active snapshot")
    calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    universe = next((item for item in state.universes if item.content_hash == active.universe_hash), None)
    if calendar is None or universe is None:
        raise HistoryPartitionRepackError("history repack source snapshot parents are unavailable")
    if len({item.relative_path for item in active.partitions}) != requirements.expected_partition_count:
        raise HistoryPartitionRepackError("history repack source partition count is invalid")
    if len(calendar.open_dates) != requirements.expected_trading_days:
        raise HistoryPartitionRepackError("history repack source trading-day count is invalid")
    files = _source_files(root, active)
    return _SourceContext(
        state,
        active,
        files,
        CanonicalDigest(files),
        sum(item.size_bytes for item in files if item.relative_path != _CONTROL_NAME),
        len(universe.securities),
        len(calendar.open_dates),
    )


def _source_files(root: Path, active: HistoryActiveSnapshot) -> tuple[HistoryPartitionRepackSourceFileIdentity, ...]:
    paths = tuple(sorted({_CONTROL_NAME, *(item.relative_path for item in active.partitions)}))
    values: list[HistoryPartitionRepackSourceFileIdentity] = []
    for relative in paths:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise HistoryPartitionRepackError("history repack source file is missing or unsafe")
        stat = path.stat()
        values.append(HistoryPartitionRepackSourceFileIdentity(relative, stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(values, key=lambda item: item.relative_path))


def _require_clean_source(root: Path) -> None:
    forbidden = tuple(
        path
        for path in root.rglob("*")
        if path.is_file() and (".pending" in path.name or path.name.endswith(".rollback.sqlite3"))
    )
    sidecars = tuple(root.rglob("*.sqlite3-wal"))
    if forbidden or any(path.stat().st_size > 0 for path in sidecars):
        raise HistoryPartitionRepackError("history repack source has pending SQLite or rollback files")


def _require_same_filesystem(layout: _RepackLayout) -> None:
    if layout.source_root.stat().st_dev != layout.target_root.parent.stat().st_dev:
        raise HistoryPartitionRepackError("history repack source and target are on different filesystems")


def _require_free_space(
    layout: _RepackLayout,
    context: _SourceContext,
    requirements: HistoryPartitionRepackRequirements,
) -> None:
    largest = max(item.size_bytes for item in context.source_files if item.relative_path != _CONTROL_NAME)
    target_bound = min(context.source_bytes, requirements.maximum_target_bytes)
    required = target_bound + largest + requirements.reserve_bytes
    if shutil.disk_usage(layout.target_root.parent).free < required:
        raise HistoryPartitionRepackError("history repack disk space is insufficient")


def _require_state_source(
    state: HistoryPartitionRepackBuildState,
    layout: _RepackLayout,
    context: _SourceContext,
    requirements: HistoryPartitionRepackRequirements,
) -> None:
    if (
        state.source_root != str(layout.source_root)
        or state.target_root != str(layout.target_root)
        or state.source_snapshot_hash != context.active.content_hash
        or state.source_sequence != context.active.sequence
        or state.source_file_identity_hash != context.source_file_identity_hash
        or state.source_files != context.source_files
        or state.expected_partition_count != requirements.expected_partition_count
        or state.page_size != requirements.target_page_size
    ):
        raise HistoryPartitionRepackError("history repack resumable state does not match the source")


def _require_source_unchanged(root: Path, state: HistoryPartitionRepackBuildState) -> None:
    for expected in state.source_files:
        path = root / expected.relative_path
        if not path.is_file() or path.is_symlink():
            raise HistoryPartitionRepackError("history repack source changed during build")
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (expected.size_bytes, expected.modified_ns):
            raise HistoryPartitionRepackError("history repack source changed during build")


def _repack_partition(
    source_root: Path,
    target_root: Path,
    reference: HistorySnapshotPartition,
    snapshot_sequence: int,
    page_size: int,
) -> HistoryPartitionRepackPartitionEvidence:
    source = source_root / reference.relative_path
    destination = target_root / reference.relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(f".{destination.stem}.repack.pending.sqlite3")
    if pending.exists():
        if not pending.is_file() or pending.is_symlink():
            raise HistoryPartitionRepackError("history repack pending partition is unsafe")
        pending.unlink()
    source_sha = _sha256_file(source)
    HistoryPricePartition.verify(source, reference)
    try:
        with closing(_read_connection(source, copy_destination=True)) as connection:
            connection.run("PRAGMA page_size=8192")
            connection.run("VACUUM INTO ?", (str(pending),))
    except sqlite3.Error as exc:
        raise HistoryPartitionRepackError("history partition VACUUM INTO failed") from exc
    _fsync_file(pending)
    source_facts = _partition_facts(source, snapshot_sequence)
    target_facts = _partition_facts(pending, snapshot_sequence)
    if source_facts != target_facts:
        raise HistoryPartitionRepackError("history repack changed logical partition content")
    with closing(_read_connection(pending)) as connection:
        if tuple(connection.run("PRAGMA page_size").require_one()) != (page_size,):
            raise HistoryPartitionRepackError("history repack target page size is invalid")
        if tuple(connection.run("PRAGMA freelist_count").require_one()) != (0,):
            raise HistoryPartitionRepackError("history repack target contains free pages")
        if tuple(connection.run("PRAGMA quick_check").require_one()) != ("ok",):
            raise HistoryPartitionRepackError("history repack target quick check failed")
        if connection.run("PRAGMA foreign_key_check").fetchone() is not None:
            raise HistoryPartitionRepackError("history repack target foreign key check failed")
    target_sha = _sha256_file(pending)
    target_reference = reference
    HistoryPricePartition.verify(pending, target_reference)
    os.replace(pending, destination)
    _fsync_directory(destination.parent)
    return HistoryPartitionRepackPartitionEvidence(
        reference.relative_path,
        source_sha,
        target_sha,
        source_facts.row_count,
        source_facts.observation_count,
        source_facts.latest_row_count,
        source.stat().st_size,
        destination.stat().st_size,
        source_facts.records_hash,
        source_facts.observations_hash,
        reference.logical_hash,
        reference.row_count,
        reference.sequence,
    )


def _partition_facts(path: Path, snapshot_sequence: int) -> _PartitionFacts:
    try:
        with closing(_read_connection(path)) as connection:
            records_count, records_hash = _hash_query(connection, _TABLE_QUERIES[0][1])
            observations_count, observations_hash = _hash_query(connection, _TABLE_QUERIES[1][1])
            latest = cast(
                int,
                connection.run(
                    "SELECT COUNT(*) FROM daily_observations AS current "
                    "WHERE current.sync_sequence <= ? AND NOT EXISTS ("
                    "SELECT 1 FROM daily_observations AS newer "
                    "WHERE newer.trade_date=current.trade_date AND newer.code=current.code "
                    "AND newer.sync_sequence <= ? AND newer.sync_sequence > current.sync_sequence)",
                    (snapshot_sequence, snapshot_sequence),
                ).require_one()[0],
            )
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise HistoryPartitionRepackError("history repack partition logical scan failed") from exc
    return _PartitionFacts(records_count, observations_count, latest, records_hash, observations_hash)


def _hash_query(connection: SqliteConnection, query: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = connection.run(query)
    while rows := cursor.fetchmany(512):
        for row in rows:
            count += 1
            for value in row:
                encoded = _sqlite_value(value)
                digest.update(len(encoded).to_bytes(8, "big"))
                digest.update(encoded)
    return count, digest.hexdigest()


def _sqlite_value(value: object) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, float):
        return b"f" + value.hex().encode("ascii")
    if isinstance(value, str):
        return b"s" + value.encode("utf-8")
    if isinstance(value, bytes):
        return b"b" + value
    raise TypeError("history repack encountered an unsupported SQLite value")


def _require_size_gate(
    state: HistoryPartitionRepackBuildState,
    requirements: HistoryPartitionRepackRequirements,
) -> None:
    if len(state.partitions) != state.expected_partition_count:
        raise HistoryPartitionRepackError("history repack target is incomplete")
    reduction = 1.0 - state.target_bytes / state.source_bytes
    if state.target_bytes > requirements.maximum_target_bytes or reduction < requirements.minimum_reduction_ratio:
        raise HistoryPartitionRepackError("history repack target does not satisfy the size gate")


def _require_journal_layout(
    journal: HistoryPartitionRepackActivationJournal,
    layout: _RepackLayout,
    state: HistoryPartitionRepackBuildState,
) -> None:
    if (
        journal.source_root != str(layout.source_root)
        or journal.target_root != str(layout.target_root)
        or journal.backup_root != str(layout.backup_root)
        or journal.source_snapshot_hash != state.source_snapshot_hash
        or journal.target_snapshot_hash != state.target_snapshot_hash
    ):
        raise HistoryPartitionRepackError("history repack activation journal does not match the build")


def _require_no_activation_fence(path: Path) -> None:
    if not path.exists():
        return
    try:
        journal = ReadHistoryPartitionRepackActivationJournal(path)
    except HistoryPartitionRepackCodecError as exc:
        raise HistoryPartitionRepackFenceError("history_archive_repack_activation_pending") from exc
    if journal.fenced:
        raise HistoryPartitionRepackFenceError("history_archive_repack_activation_pending")


def _optional_journal(path: Path) -> HistoryPartitionRepackActivationJournal | None:
    return ReadHistoryPartitionRepackActivationJournal(path) if path.exists() else None


def _require_stable_snapshot(root: Path, expected_hash: str) -> None:
    try:
        active = HistoryControlIndex(root / _CONTROL_NAME).load_state().active_snapshot
    except HistoryControlError as exc:
        raise HistoryPartitionRepackError("history repack stable control is unavailable") from exc
    if active is None or active.content_hash != expected_hash:
        raise HistoryPartitionRepackError("history repack stable snapshot identity is invalid")
    _clear_empty_control_sidecars(root / _CONTROL_NAME)


def _stable_snapshot_hash(root: Path) -> str | None:
    try:
        active = HistoryControlIndex(root / _CONTROL_NAME).load_state().active_snapshot
    except (HistoryControlError, OSError):
        return None
    _clear_empty_control_sidecars(root / _CONTROL_NAME)
    return active.content_hash if active is not None else None


def _clear_empty_control_sidecars(path: Path) -> None:
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise HistoryPartitionRepackError("history repack control has pending WAL content")
    _remove_sqlite_sidecars(path)


def _clear_empty_partition_sidecars(paths: set[Path]) -> None:
    for path in paths:
        wal = Path(f"{path}-wal")
        shm = Path(f"{path}-shm")
        for sidecar in (wal, shm):
            if sidecar.exists() and (sidecar.is_symlink() or not sidecar.is_file()):
                raise HistoryPartitionRepackError("history repack partition sidecar is unsafe")
        if wal.exists() and wal.stat().st_size > 0:
            raise HistoryPartitionRepackError("history repack partition has pending WAL content")
        _remove_sqlite_sidecars(path)


def _move_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    _fsync_directory(source.parent)
    _fsync_directory(destination.parent)


def _status(action: str, state_name: str, state: HistoryPartitionRepackBuildState) -> HistoryPartitionRepackStatus:
    return HistoryPartitionRepackStatus(
        cast(HistoryPartitionRepackAction, action),
        state_name,
        state.source_snapshot_hash,
        state.target_snapshot_hash,
        len(state.partitions),
        state.expected_partition_count,
        state.source_bytes,
        state.target_bytes,
    )


def _read_connection(path: Path, *, copy_destination: bool = False) -> SqliteConnection:
    connection = OpenSqliteConnection(
        path,
        SqliteConnectionOptions(
            read_only=True, timeout_seconds=30.0, foreign_keys=True, query_only=not copy_destination
        ),
    )
    try:
        connection.run("PRAGMA cache_size=-8192")
        connection.run("PRAGMA mmap_size=0")
        connection.run("PRAGMA temp_store=FILE")
    except BaseException:
        connection.close()
        raise
    return connection


def _remove_sqlite_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    _remove_sqlite_sidecars(path)


def _remove_sqlite_sidecars(path: Path) -> None:
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY)
    try:
        while chunk := os.read(descriptor, _HASH_CHUNK_BYTES):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _fsync_file(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directory_size(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink())


__all__ = [
    "HistoryPartitionRepackCoordinator",
    "HistoryPartitionRepackError",
    "HistoryPartitionRepackFenceError",
    "RequireHistoryPartitionRepackInactive",
]
