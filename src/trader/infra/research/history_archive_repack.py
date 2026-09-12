"""Crash-safe 8 KiB physical repack of the stable BaoStock archive."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

from trader.domain.research.artifact_identity import canonical_artifact_hash
from trader.domain.research.history_control import (
    HistoryActiveSnapshot,
    HistoryControlState,
    HistorySnapshotPartition,
)
from trader.infra.research.history_archive_repack_codec import (
    HistoryArchiveRepackCodecError,
    read_history_activation_journal,
    read_history_repack_build_state,
    read_training_memory_evidence,
    write_history_activation_journal,
    write_history_repack_build_state,
)
from trader.infra.research.history_archive_repack_state import (
    HistoryArchiveActivationJournal,
    HistoryArchiveActivationState,
    HistoryArchiveRepackAction,
    HistoryArchiveRepackBuildState,
    HistoryArchiveRepackPartition,
    HistoryArchiveRepackRequirements,
    HistoryArchiveRepackStatus,
    HistoryArchiveSourceFile,
)
from trader.infra.research.history_control_repository import (
    HistoryControlError,
    HistoryMaintenanceLock,
    SQLiteHistoryControlRepository,
)
from trader.infra.research.history_month_partition import SQLiteHistoryMonthPartitionRepository
from trader.infra.scoring.profiles.v3.training_bundle_repository import inspect_active_tomorrow_bundle

RepackProgress = Callable[[int, int, str], None]
FaultInjector = Callable[[str], None]
_BUILD_STATE_NAME = "repack-state.json"
_ACTIVATION_JOURNAL_NAME = "baostock-repack-activation.json"
_CONTROL_NAME = "control.sqlite3"
_PARTITIONS_NAME = "partitions"
_HASH_CHUNK_BYTES = 1024 * 1024
_TABLE_QUERIES = (
    (
        "daily_records",
        "SELECT trade_date, code, revision_id, first_seen_sequence, board, payload_json, content_hash "
        "FROM daily_records ORDER BY trade_date, code, revision_id",
    ),
    (
        "daily_observations",
        "SELECT trade_date, code, sync_sequence, revision_id FROM daily_observations "
        "ORDER BY trade_date, code, sync_sequence",
    ),
)


class HistoryArchiveRepackError(RuntimeError):
    """The physical archive repack cannot safely continue."""


class HistoryArchiveRepackFenceError(HistoryArchiveRepackError):
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
    source_files: tuple[HistoryArchiveSourceFile, ...]
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


class HistoryArchiveRepackCoordinator:
    """Build, activate, roll back and finalize one physical archive repack."""

    def __init__(
        self,
        source_root: Path,
        target_root: Path,
        *,
        requirements: HistoryArchiveRepackRequirements | None = None,
        progress: RepackProgress | None = None,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        source = source_root.expanduser().resolve()
        target = target_root.expanduser().resolve()
        self._layout = _validate_layout(source, target)
        self._requirements = requirements or HistoryArchiveRepackRequirements()
        self._progress = progress
        self._inject = fault_injector or (lambda _stage: None)

    def build(self) -> HistoryArchiveRepackStatus:
        layout = self._layout
        layout.target_root.parent.mkdir(parents=True, exist_ok=True)
        with HistoryMaintenanceLock(layout.source_root / ".maintenance.lock"):
            _require_no_activation_fence(layout.journal_path)
            context = _load_source_context(layout.source_root, self._requirements)
            _require_same_filesystem(layout)
            state = self._load_or_create_build_state(context)
            if state.completed:
                _verify_completed_target(layout.target_root, state)
                return _status("build", "completed", state)
            _discard_incomplete_outputs(layout.target_root, context.active.partitions, state)
            _require_target_layout(layout.target_root, state)
            state = self._resume_completed_partitions(state)
            for reference in context.active.partitions:
                if _partition_evidence(state, reference.relative_path) is not None:
                    continue
                evidence = _repack_partition(
                    layout.source_root,
                    layout.target_root,
                    reference,
                    context.active.sequence,
                    self._requirements.target_page_size,
                )
                self._inject(f"partition_replaced:{reference.relative_path}")
                state = replace(
                    state,
                    partitions=tuple(sorted((*state.partitions, evidence), key=lambda item: item.relative_path)),
                )
                write_history_repack_build_state(layout.state_path, state)
                self._publish_progress(len(state.partitions), state.expected_partition_count, reference.relative_path)
            _require_source_unchanged(layout.source_root, state)
            _require_size_gate(state, self._requirements)
            target_snapshot = _build_target_control(layout, context, state)
            self._inject("target_control_built")
            state = replace(state, target_snapshot_hash=target_snapshot.content_hash, completed=True)
            write_history_repack_build_state(layout.state_path, state)
            _verify_completed_target(layout.target_root, state)
            self._inject("build_completed")
            return _status("build", "completed", state)

    def activate(self) -> HistoryArchiveRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root / ".maintenance.lock"):
            state = read_history_repack_build_state(layout.state_path)
            _require_completed_state(state, layout)
            existing = _optional_journal(layout.journal_path)
            if existing is not None and existing.state not in {"rolled_back", "finalized"}:
                recovered = self._recover_activation(existing, state)
                return _activation_status("activate", recovered.state, state)
            if existing is not None and existing.state == "finalized":
                return _activation_status("activate", "finalized", state)
            if layout.backup_root.exists():
                raise HistoryArchiveRepackError("history repack backup target already exists")
            _verify_completed_target(layout.target_root, state)
            _require_stable_snapshot(layout.source_root, state.source_snapshot_hash)
            layout.backup_root.mkdir(parents=True)
            _fsync_directory(layout.backup_root.parent)
            journal = HistoryArchiveActivationJournal(
                "prepared",
                str(layout.source_root),
                str(layout.target_root),
                str(layout.backup_root),
                state.source_snapshot_hash,
                cast(str, state.target_snapshot_hash),
            )
            write_history_activation_journal(layout.journal_path, journal)
            self._inject("prepared")
            journal = self._move_and_commit(
                layout.source_root / _PARTITIONS_NAME,
                layout.backup_root / _PARTITIONS_NAME,
                journal,
                "old_partitions_moved",
            )
            journal = self._move_and_commit(
                layout.source_root / _CONTROL_NAME,
                layout.backup_root / _CONTROL_NAME,
                journal,
                "old_control_moved",
            )
            journal = self._move_and_commit(
                layout.target_root / _PARTITIONS_NAME,
                layout.source_root / _PARTITIONS_NAME,
                journal,
                "new_partitions_activated",
            )
            journal = self._move_and_commit(
                layout.target_root / _CONTROL_NAME,
                layout.source_root / _CONTROL_NAME,
                journal,
                "new_control_activated",
            )
            _require_stable_target(layout, state, full_hash=False)
            journal = replace(journal, state="verified")
            write_history_activation_journal(layout.journal_path, journal)
            self._inject("verified")
            return _activation_status("activate", "verified", state)

    def rollback(self) -> HistoryArchiveRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root / ".maintenance.lock"):
            state = read_history_repack_build_state(layout.state_path)
            _require_completed_state(state, layout)
            journal = read_history_activation_journal(layout.journal_path)
            _require_journal_layout(journal, layout, state)
            if journal.state == "finalized":
                raise HistoryArchiveRepackError("finalized history repack cannot be rolled back")
            if journal.state == "rolled_back":
                _require_stable_source(layout, state, full_hash=False)
                return _activation_status("rollback", "rolled_back", state)
            recovered = self._restore_source_archive(journal, state)
            return _activation_status("rollback", recovered.state, state)

    def finalize(self, training_root: Path, memory_evidence_path: Path) -> HistoryArchiveRepackStatus:
        layout = self._layout
        with HistoryMaintenanceLock(layout.source_root / ".maintenance.lock"):
            state = read_history_repack_build_state(layout.state_path)
            _require_completed_state(state, layout)
            journal = read_history_activation_journal(layout.journal_path)
            _require_journal_layout(journal, layout, state)
            if journal.state == "finalized":
                return _activation_status("finalize", "finalized", state)
            if journal.state != "verified":
                raise HistoryArchiveRepackError("history repack must be verified before finalization")
            _require_stable_target(layout, state, full_hash=False)
            bundle = inspect_active_tomorrow_bundle(training_root.resolve() / "tomorrow-v3")
            evidence = read_training_memory_evidence(memory_evidence_path.resolve())
            if (
                bundle.training_input_hash != state.target_snapshot_hash
                or evidence.training_input_hash != state.target_snapshot_hash
                or evidence.model_hash != bundle.model_hash
                or evidence.report_hash != bundle.report_hash
            ):
                raise HistoryArchiveRepackError("training evidence does not match the activated archive and bundle")
            released = _directory_size(layout.backup_root)
            _require_safe_backup(layout, state)
            shutil.rmtree(layout.backup_root)
            _fsync_directory(layout.backup_root.parent)
            journal = replace(journal, state="finalized")
            write_history_activation_journal(layout.journal_path, journal)
            self._inject("finalized")
            return _activation_status("finalize", "finalized", state, released)

    def _load_or_create_build_state(self, context: _SourceContext) -> HistoryArchiveRepackBuildState:
        layout = self._layout
        if layout.state_path.exists():
            state = read_history_repack_build_state(layout.state_path)
            _require_state_source(state, layout, context, self._requirements)
            return state
        if layout.target_root.exists() and any(layout.target_root.iterdir()):
            raise HistoryArchiveRepackError("history repack target is not empty and has no trusted state")
        _require_free_space(layout, context, self._requirements)
        layout.target_root.mkdir(parents=True, exist_ok=True)
        state = HistoryArchiveRepackBuildState(
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
        write_history_repack_build_state(layout.state_path, state)
        self._inject("build_prepared")
        return state

    def _resume_completed_partitions(self, state: HistoryArchiveRepackBuildState) -> HistoryArchiveRepackBuildState:
        for item in state.partitions:
            path = self._layout.target_root / item.relative_path
            reference = HistorySnapshotPartition(item.relative_path, item.target_sha256, item.row_count)
            SQLiteHistoryMonthPartitionRepository.verify(path, reference)
            with closing(_read_connection(path)) as connection:
                if connection.execute("PRAGMA page_size").fetchone() != (state.page_size,):
                    raise HistoryArchiveRepackError("resumed history repack partition page size is invalid")
                if connection.execute("PRAGMA freelist_count").fetchone() != (0,):
                    raise HistoryArchiveRepackError("resumed history repack partition has free pages")
        return state

    def _move_and_commit(
        self,
        source: Path,
        destination: Path,
        journal: HistoryArchiveActivationJournal,
        next_state: HistoryArchiveActivationState,
    ) -> HistoryArchiveActivationJournal:
        if not source.exists() or destination.exists() or source.is_symlink():
            raise HistoryArchiveRepackError("history repack activation path is inconsistent")
        os.replace(source, destination)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
        self._inject(f"{next_state}_moved")
        updated = replace(journal, state=next_state)
        write_history_activation_journal(self._layout.journal_path, updated)
        self._inject(next_state)
        return updated

    def _recover_activation(
        self,
        journal: HistoryArchiveActivationJournal,
        state: HistoryArchiveRepackBuildState,
    ) -> HistoryArchiveActivationJournal:
        _require_journal_layout(journal, self._layout, state)
        if _stable_target_matches(self._layout, state, full_hash=False):
            recovered = replace(journal, state="verified")
            write_history_activation_journal(self._layout.journal_path, recovered)
            return recovered
        return self._restore_source_archive(journal, state)

    def _restore_source_archive(
        self,
        journal: HistoryArchiveActivationJournal,
        state: HistoryArchiveRepackBuildState,
    ) -> HistoryArchiveActivationJournal:
        layout = self._layout
        _return_new_control(layout, state)
        _return_new_partitions(layout, state)
        _restore_old_path(layout.backup_root / _PARTITIONS_NAME, layout.source_root / _PARTITIONS_NAME)
        _restore_old_path(layout.backup_root / _CONTROL_NAME, layout.source_root / _CONTROL_NAME)
        _require_stable_source(layout, state, full_hash=True)
        if layout.backup_root.exists():
            if any(layout.backup_root.iterdir()):
                raise HistoryArchiveRepackError("history repack backup was not fully restored")
            layout.backup_root.rmdir()
            _fsync_directory(layout.backup_root.parent)
        recovered = replace(journal, state="rolled_back")
        write_history_activation_journal(layout.journal_path, recovered)
        self._inject("rolled_back")
        return recovered

    def _publish_progress(self, completed: int, total: int, current: str) -> None:
        if self._progress is not None:
            self._progress(completed, total, current)


def require_history_repack_inactive(archive_root: Path) -> None:
    """Fail normal history work while a conventional repack activation is fenced."""

    root = archive_root.expanduser().resolve()
    if root.name != "baostock" or root.parent.name != "history":
        return
    journal_path = root.parent.parent / "historyless" / _ACTIVATION_JOURNAL_NAME
    _require_no_activation_fence(journal_path)


def _validate_layout(source: Path, target: Path) -> _RepackLayout:
    if (
        source.name != "baostock"
        or target.name != "baostock"
        or source.parent.name != "history"
        or target.parent.name != "historyless"
        or source.parent.parent != target.parent.parent
        or source == target
        or source in target.parents
        or target in source.parents
    ):
        raise HistoryArchiveRepackError(
            "history repack roots must be sibling data/history and data/historyless archives"
        )
    return _RepackLayout(
        source,
        target,
        target.parent / "baostock-before-repack",
        target / _BUILD_STATE_NAME,
        target.parent / _ACTIVATION_JOURNAL_NAME,
    )


def _load_source_context(root: Path, requirements: HistoryArchiveRepackRequirements) -> _SourceContext:
    if not root.is_dir() or root.is_symlink():
        raise HistoryArchiveRepackError("history repack source archive is missing or unsafe")
    _require_clean_source(root)
    control = SQLiteHistoryControlRepository(root / _CONTROL_NAME)
    try:
        if control.integrity().state != "healthy":
            raise HistoryArchiveRepackError("history repack source control is unavailable")
        state = control.load_state()
    except HistoryControlError as exc:
        raise HistoryArchiveRepackError("history repack source control is unavailable") from exc
    active = state.active_snapshot
    if active is None:
        raise HistoryArchiveRepackError("history repack source has no active snapshot")
    calendar = next((item for item in state.calendars if item.content_hash == active.calendar_hash), None)
    universe = next((item for item in state.universes if item.content_hash == active.universe_hash), None)
    if calendar is None or universe is None:
        raise HistoryArchiveRepackError("history repack source snapshot parents are unavailable")
    if len(active.partitions) != requirements.expected_partition_count:
        raise HistoryArchiveRepackError("history repack source partition count is invalid")
    if len(calendar.open_dates) != requirements.expected_trading_days:
        raise HistoryArchiveRepackError("history repack source trading-day count is invalid")
    files = _source_files(root, active)
    return _SourceContext(
        state,
        active,
        files,
        canonical_artifact_hash(files),
        sum(item.size_bytes for item in files if item.relative_path != _CONTROL_NAME),
        len(universe.securities),
        len(calendar.open_dates),
    )


def _source_files(root: Path, active: HistoryActiveSnapshot) -> tuple[HistoryArchiveSourceFile, ...]:
    paths = (_CONTROL_NAME, *(item.relative_path for item in active.partitions))
    values: list[HistoryArchiveSourceFile] = []
    for relative in paths:
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise HistoryArchiveRepackError("history repack source file is missing or unsafe")
        stat = path.stat()
        values.append(HistoryArchiveSourceFile(relative, stat.st_size, stat.st_mtime_ns))
    return tuple(sorted(values, key=lambda item: item.relative_path))


def _require_clean_source(root: Path) -> None:
    forbidden = tuple(
        path
        for path in root.rglob("*")
        if path.is_file() and (".pending" in path.name or path.name.endswith(".rollback.sqlite3"))
    )
    sidecars = tuple(root.rglob("*.sqlite3-wal"))
    if forbidden or any(path.stat().st_size > 0 for path in sidecars):
        raise HistoryArchiveRepackError("history repack source has pending SQLite or rollback files")


def _require_same_filesystem(layout: _RepackLayout) -> None:
    if layout.source_root.stat().st_dev != layout.target_root.parent.stat().st_dev:
        raise HistoryArchiveRepackError("history repack source and target are on different filesystems")


def _require_free_space(
    layout: _RepackLayout,
    context: _SourceContext,
    requirements: HistoryArchiveRepackRequirements,
) -> None:
    largest = max(item.size_bytes for item in context.source_files if item.relative_path != _CONTROL_NAME)
    target_bound = min(context.source_bytes, requirements.maximum_target_bytes)
    required = target_bound + largest + requirements.reserve_bytes
    if shutil.disk_usage(layout.target_root.parent).free < required:
        raise HistoryArchiveRepackError("history repack disk space is insufficient")


def _require_state_source(
    state: HistoryArchiveRepackBuildState,
    layout: _RepackLayout,
    context: _SourceContext,
    requirements: HistoryArchiveRepackRequirements,
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
        raise HistoryArchiveRepackError("history repack resumable state does not match the source")


def _require_source_unchanged(root: Path, state: HistoryArchiveRepackBuildState) -> None:
    for expected in state.source_files:
        path = root / expected.relative_path
        if not path.is_file() or path.is_symlink():
            raise HistoryArchiveRepackError("history repack source changed during build")
        stat = path.stat()
        if (stat.st_size, stat.st_mtime_ns) != (expected.size_bytes, expected.modified_ns):
            raise HistoryArchiveRepackError("history repack source changed during build")


def _require_target_layout(root: Path, state: HistoryArchiveRepackBuildState) -> None:
    allowed = {root / _BUILD_STATE_NAME}
    if state.completed:
        allowed.add(root / _CONTROL_NAME)
    for item in state.partitions:
        allowed.add(root / item.relative_path)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HistoryArchiveRepackError("history repack target contains a symbolic link")
        if path.is_dir() or path in allowed:
            continue
        raise HistoryArchiveRepackError("history repack target contains an unknown file")


def _discard_incomplete_outputs(
    root: Path,
    references: Sequence[HistorySnapshotPartition],
    state: HistoryArchiveRepackBuildState,
) -> None:
    completed = frozenset(item.relative_path for item in state.partitions)
    for reference in references:
        if reference.relative_path in completed:
            continue
        destination = root / reference.relative_path
        pending = destination.with_name(f".{destination.stem}.repack.pending.sqlite3")
        for candidate in (destination, pending):
            if not candidate.exists():
                continue
            if not candidate.is_file() or candidate.is_symlink():
                raise HistoryArchiveRepackError("history repack incomplete partition is unsafe")
            candidate.unlink()
            _fsync_directory(candidate.parent)
    control = root / _CONTROL_NAME
    if control.exists():
        if len(state.partitions) != state.expected_partition_count or not control.is_file() or control.is_symlink():
            raise HistoryArchiveRepackError("history repack uncommitted target control is unsafe")
        _remove_sqlite_files(control)
        _fsync_directory(root)


def _repack_partition(
    source_root: Path,
    target_root: Path,
    reference: HistorySnapshotPartition,
    snapshot_sequence: int,
    page_size: int,
) -> HistoryArchiveRepackPartition:
    source = source_root / reference.relative_path
    destination = target_root / reference.relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(f".{destination.stem}.repack.pending.sqlite3")
    if pending.exists():
        if not pending.is_file() or pending.is_symlink():
            raise HistoryArchiveRepackError("history repack pending partition is unsafe")
        pending.unlink()
    source_sha = _sha256_file(source)
    if source_sha != reference.sha256:
        raise HistoryArchiveRepackError("history repack source partition hash mismatch")
    try:
        with closing(_read_connection(source)) as connection:
            connection.execute("PRAGMA page_size=8192")
            connection.execute("VACUUM INTO ?", (str(pending),))
    except sqlite3.Error as exc:
        raise HistoryArchiveRepackError("history partition VACUUM INTO failed") from exc
    _fsync_file(pending)
    source_facts = _partition_facts(source, snapshot_sequence)
    target_facts = _partition_facts(pending, snapshot_sequence)
    if source_facts != target_facts:
        raise HistoryArchiveRepackError("history repack changed logical partition content")
    with closing(_read_connection(pending)) as connection:
        if connection.execute("PRAGMA page_size").fetchone() != (page_size,):
            raise HistoryArchiveRepackError("history repack target page size is invalid")
        if connection.execute("PRAGMA freelist_count").fetchone() != (0,):
            raise HistoryArchiveRepackError("history repack target contains free pages")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise HistoryArchiveRepackError("history repack target quick check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise HistoryArchiveRepackError("history repack target foreign key check failed")
    target_sha = _sha256_file(pending)
    target_reference = HistorySnapshotPartition(reference.relative_path, target_sha, target_facts.row_count)
    SQLiteHistoryMonthPartitionRepository.verify(pending, target_reference)
    os.replace(pending, destination)
    _fsync_directory(destination.parent)
    return HistoryArchiveRepackPartition(
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
    )


def _partition_facts(path: Path, snapshot_sequence: int) -> _PartitionFacts:
    try:
        with closing(_read_connection(path)) as connection:
            records_count, records_hash = _hash_query(connection, _TABLE_QUERIES[0][1])
            observations_count, observations_hash = _hash_query(connection, _TABLE_QUERIES[1][1])
            latest = cast(
                int,
                connection.execute(
                    "SELECT COUNT(*) FROM daily_observations AS current "
                    "WHERE current.sync_sequence <= ? AND NOT EXISTS ("
                    "SELECT 1 FROM daily_observations AS newer "
                    "WHERE newer.trade_date=current.trade_date AND newer.code=current.code "
                    "AND newer.sync_sequence <= ? AND newer.sync_sequence > current.sync_sequence)",
                    (snapshot_sequence, snapshot_sequence),
                ).fetchone()[0],
            )
    except (sqlite3.Error, TypeError, ValueError) as exc:
        raise HistoryArchiveRepackError("history repack partition logical scan failed") from exc
    return _PartitionFacts(records_count, observations_count, latest, records_hash, observations_hash)


def _hash_query(connection: sqlite3.Connection, query: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    cursor = connection.execute(query)
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


def _build_target_control(
    layout: _RepackLayout,
    context: _SourceContext,
    state: HistoryArchiveRepackBuildState,
) -> HistoryActiveSnapshot:
    target_control = layout.target_root / _CONTROL_NAME
    temporary = target_control.with_name(f".{_CONTROL_NAME}.repack.pending")
    _remove_sqlite_files(temporary)
    shutil.copyfile(layout.source_root / _CONTROL_NAME, temporary)
    _fsync_file(temporary)
    references = tuple(
        HistorySnapshotPartition(item.relative_path, item.target_sha256, item.row_count) for item in state.partitions
    )
    target_snapshot = HistoryActiveSnapshot(
        context.active.sequence + 1,
        context.active.data_cutoff,
        context.active.label_cutoff,
        context.active.calendar_hash,
        context.active.universe_hash,
        context.active.source_identity_hash,
        references,
    )
    repository = SQLiteHistoryControlRepository(temporary)
    repository.publish_snapshot(target_snapshot)
    _checkpoint_sqlite(temporary)
    os.replace(temporary, target_control)
    _fsync_directory(target_control.parent)
    active = SQLiteHistoryControlRepository(target_control).load_state().active_snapshot
    if active != target_snapshot:
        raise HistoryArchiveRepackError("history repack target control publication failed")
    return target_snapshot


def _require_size_gate(
    state: HistoryArchiveRepackBuildState,
    requirements: HistoryArchiveRepackRequirements,
) -> None:
    if len(state.partitions) != state.expected_partition_count:
        raise HistoryArchiveRepackError("history repack target is incomplete")
    reduction = 1.0 - state.target_bytes / state.source_bytes
    if state.target_bytes > requirements.maximum_target_bytes or reduction < requirements.minimum_reduction_ratio:
        raise HistoryArchiveRepackError("history repack target does not satisfy the size gate")


def _verify_completed_target(root: Path, state: HistoryArchiveRepackBuildState) -> None:
    if not state.completed or state.target_snapshot_hash is None:
        raise HistoryArchiveRepackError("history repack build is not complete")
    _require_stable_snapshot(root, state.target_snapshot_hash)
    _require_target_layout(root, state)
    _require_partition_set(root / _PARTITIONS_NAME, _target_references(state), full_hash=False)


def _require_completed_state(state: HistoryArchiveRepackBuildState, layout: _RepackLayout) -> None:
    if (
        not state.completed
        or state.target_snapshot_hash is None
        or state.source_root != str(layout.source_root)
        or state.target_root != str(layout.target_root)
    ):
        raise HistoryArchiveRepackError("history repack completed state is invalid")


def _partition_evidence(
    state: HistoryArchiveRepackBuildState,
    relative_path: str,
) -> HistoryArchiveRepackPartition | None:
    return next((item for item in state.partitions if item.relative_path == relative_path), None)


def _target_references(state: HistoryArchiveRepackBuildState) -> tuple[HistorySnapshotPartition, ...]:
    return tuple(
        HistorySnapshotPartition(item.relative_path, item.target_sha256, item.row_count) for item in state.partitions
    )


def _source_references(state: HistoryArchiveRepackBuildState) -> tuple[HistorySnapshotPartition, ...]:
    return tuple(
        HistorySnapshotPartition(item.relative_path, item.source_sha256, item.row_count) for item in state.partitions
    )


def _require_journal_layout(
    journal: HistoryArchiveActivationJournal,
    layout: _RepackLayout,
    state: HistoryArchiveRepackBuildState,
) -> None:
    if (
        journal.source_root != str(layout.source_root)
        or journal.target_root != str(layout.target_root)
        or journal.backup_root != str(layout.backup_root)
        or journal.source_snapshot_hash != state.source_snapshot_hash
        or journal.target_snapshot_hash != state.target_snapshot_hash
    ):
        raise HistoryArchiveRepackError("history repack activation journal does not match the build")


def _require_no_activation_fence(path: Path) -> None:
    if not path.exists():
        return
    try:
        journal = read_history_activation_journal(path)
    except HistoryArchiveRepackCodecError as exc:
        raise HistoryArchiveRepackFenceError("history_repack_activation_pending") from exc
    if journal.fenced:
        raise HistoryArchiveRepackFenceError("history_repack_activation_pending")


def _optional_journal(path: Path) -> HistoryArchiveActivationJournal | None:
    return read_history_activation_journal(path) if path.exists() else None


def _require_stable_snapshot(root: Path, expected_hash: str) -> None:
    try:
        active = SQLiteHistoryControlRepository(root / _CONTROL_NAME).load_state().active_snapshot
    except HistoryControlError as exc:
        raise HistoryArchiveRepackError("history repack stable control is unavailable") from exc
    if active is None or active.content_hash != expected_hash:
        raise HistoryArchiveRepackError("history repack stable snapshot identity is invalid")
    _clear_empty_control_sidecars(root / _CONTROL_NAME)


def _stable_snapshot_hash(root: Path) -> str | None:
    try:
        active = SQLiteHistoryControlRepository(root / _CONTROL_NAME).load_state().active_snapshot
    except (HistoryControlError, OSError):
        return None
    _clear_empty_control_sidecars(root / _CONTROL_NAME)
    return active.content_hash if active is not None else None


def _clear_empty_control_sidecars(path: Path) -> None:
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise HistoryArchiveRepackError("history repack control has pending WAL content")
    _remove_sqlite_sidecars(path)


def _stable_target_matches(layout: _RepackLayout, state: HistoryArchiveRepackBuildState, *, full_hash: bool) -> bool:
    try:
        _require_stable_target(layout, state, full_hash=full_hash)
    except (HistoryArchiveRepackError, OSError, ValueError):
        return False
    return True


def _require_stable_target(layout: _RepackLayout, state: HistoryArchiveRepackBuildState, *, full_hash: bool) -> None:
    _require_stable_snapshot(layout.source_root, cast(str, state.target_snapshot_hash))
    _require_partition_set(layout.source_root / _PARTITIONS_NAME, _target_references(state), full_hash=full_hash)


def _require_stable_source(layout: _RepackLayout, state: HistoryArchiveRepackBuildState, *, full_hash: bool) -> None:
    _require_stable_snapshot(layout.source_root, state.source_snapshot_hash)
    _require_partition_set(layout.source_root / _PARTITIONS_NAME, _source_references(state), full_hash=full_hash)


def _require_partition_set(
    root: Path,
    references: Sequence[HistorySnapshotPartition],
    *,
    full_hash: bool,
) -> None:
    if not root.is_dir() or root.is_symlink():
        raise HistoryArchiveRepackError("history repack partition directory is unavailable")
    expected_files = {root.parent / item.relative_path for item in references}
    expected_directories = {root}
    for path in expected_files:
        expected_directories.update(path.parents)
    expected_directories = {path for path in expected_directories if path == root or root in path.parents}
    actual_files = _require_exact_partition_tree(root, expected_files, expected_directories)
    if actual_files != expected_files:
        raise HistoryArchiveRepackError("history repack partition path set is invalid")
    for reference in references:
        path = root.parent / reference.relative_path
        if full_hash and _sha256_file(path) != reference.sha256:
            raise HistoryArchiveRepackError("history repack partition hash is invalid")


def _require_exact_partition_tree(
    root: Path,
    expected_files: set[Path],
    expected_directories: set[Path],
) -> set[Path]:
    actual_files: set[Path] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise HistoryArchiveRepackError("history repack partition tree contains a symbolic link")
        if path.is_dir():
            if path not in expected_directories:
                raise HistoryArchiveRepackError("history repack partition tree contains an unknown directory")
            continue
        if not path.is_file() or path not in expected_files:
            raise HistoryArchiveRepackError("history repack partition tree contains an unknown file")
        actual_files.add(path)
    return actual_files


def _return_new_control(layout: _RepackLayout, state: HistoryArchiveRepackBuildState) -> None:
    source = layout.source_root / _CONTROL_NAME
    target = layout.target_root / _CONTROL_NAME
    identity = _stable_snapshot_hash(layout.source_root) if source.exists() else None
    if identity == state.target_snapshot_hash:
        if target.exists():
            raise HistoryArchiveRepackError("history repack target control already exists")
        _move_path(source, target)
    elif identity not in {None, state.source_snapshot_hash}:
        raise HistoryArchiveRepackError("history repack stable control identity is unknown")


def _return_new_partitions(layout: _RepackLayout, state: HistoryArchiveRepackBuildState) -> None:
    source = layout.source_root / _PARTITIONS_NAME
    target = layout.target_root / _PARTITIONS_NAME
    if not source.exists():
        return
    try:
        _require_partition_set(source, _target_references(state), full_hash=True)
    except HistoryArchiveRepackError:
        _require_partition_set(source, _source_references(state), full_hash=True)
        return
    if target.exists():
        raise HistoryArchiveRepackError("history repack target partitions already exist")
    _move_path(source, target)


def _restore_old_path(source: Path, destination: Path) -> None:
    if destination.exists():
        return
    if not source.exists() or source.is_symlink():
        raise HistoryArchiveRepackError("history repack rollback evidence is unavailable")
    _move_path(source, destination)


def _move_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    _fsync_directory(source.parent)
    _fsync_directory(destination.parent)


def _require_safe_backup(layout: _RepackLayout, state: HistoryArchiveRepackBuildState) -> None:
    if (
        layout.backup_root.parent != layout.target_root.parent
        or layout.backup_root.name != "baostock-before-repack"
        or not layout.backup_root.is_dir()
        or layout.backup_root.is_symlink()
    ):
        raise HistoryArchiveRepackError("history repack backup path is unsafe")
    _require_stable_snapshot(layout.backup_root, state.source_snapshot_hash)
    _require_partition_set(layout.backup_root / _PARTITIONS_NAME, _source_references(state), full_hash=True)


def _status(action: str, state_name: str, state: HistoryArchiveRepackBuildState) -> HistoryArchiveRepackStatus:
    return HistoryArchiveRepackStatus(
        cast(HistoryArchiveRepackAction, action),
        state_name,
        state.source_snapshot_hash,
        state.target_snapshot_hash,
        len(state.partitions),
        state.expected_partition_count,
        state.source_bytes,
        state.target_bytes,
    )


def _activation_status(
    action: str,
    state_name: str,
    state: HistoryArchiveRepackBuildState,
    released: int = 0,
) -> HistoryArchiveRepackStatus:
    return replace(_status(action, state_name, state), released_bytes=released)


def _read_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30.0)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA cache_size=-8192")
    connection.execute("PRAGMA mmap_size=0")
    connection.execute("PRAGMA temp_store=FILE")
    return connection


def _checkpoint_sqlite(path: Path) -> None:
    try:
        with closing(sqlite3.connect(path, timeout=30.0)) as connection:
            result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.Error as exc:
        raise HistoryArchiveRepackError("history repack control checkpoint failed") from exc
    if result is None or result[0] != 0:
        raise HistoryArchiveRepackError("history repack control checkpoint did not complete")
    _remove_sqlite_sidecars(path)
    _fsync_file(path)


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
    "HistoryArchiveRepackCoordinator",
    "HistoryArchiveRepackError",
    "HistoryArchiveRepackFenceError",
    "require_history_repack_inactive",
]
