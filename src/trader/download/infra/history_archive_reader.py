"""Date-routed reader for an active set of monthly history partitions."""

from __future__ import annotations

import calendar
import heapq
import re
import threading
from collections import deque
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.domain.baostock_daily import BAOSTOCK_MAX_SESSIONS
from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_revision import (
    HISTORY_TRAINING_WINDOW_SESSIONS,
    MAX_HISTORY_TRAINING_WINDOW_SESSIONS,
    HistoryRevision,
    HistoryTrainingPoint,
    HistoryTrainingWindow,
)
from trader.download.domain.published_history import PublishedHistoryCell
from trader.download.infra.history_control_repository import HistoryControlError, SQLiteHistoryControlRepository
from trader.download.infra.history_month_partition import (
    HistoryMonthPartitionError,
    HistoryPartitionVerificationPhase,
    SQLiteHistoryMonthPartitionRepository,
)
from trader.download.infra.history_reference_files import HistoryReferenceIndex, read_history_reference

_CODE = re.compile(r"^[0-9]{6}$")


class HistoryArchiveReadError(RuntimeError):
    """The active monthly history view is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class _PartitionFileIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


@dataclass(frozen=True, slots=True)
class _VerifiedPartition:
    reference: HistorySnapshotPartition
    identity: _PartitionFileIdentity
    repository: SQLiteHistoryMonthPartitionRepository


def _partition_file_identity(path: Path) -> _PartitionFileIdentity:
    stat = path.stat()
    wal = Path(f"{path}-wal")
    if wal.exists() and wal.stat().st_size > 0:
        raise HistoryArchiveReadError("history snapshot partition has pending WAL")
    return _PartitionFileIdentity(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


@dataclass(frozen=True)
class HistoryPartitionRevisionComparison:
    """Compare two logical sequences through one trusted physical partition."""

    physical_reference: HistorySnapshotPartition
    before_sequence: int
    after_sequence: int

    def __post_init__(self) -> None:
        if self.before_sequence < 1 or self.after_sequence <= self.before_sequence:
            raise ValueError("history partition revision comparison sequences are invalid")


def route_history_months(start: date, end: date) -> tuple[tuple[int, int], ...]:
    if start > end:
        raise ValueError("history month route is invalid")
    year = start.year
    month = start.month
    routed: list[tuple[int, int]] = []
    while (year, month) <= (end.year, end.month):
        routed.append((year, month))
        if month == 12:
            year += 1
            month = 1
        else:
            month += 1
    return tuple(routed)


class SQLiteHistoryArchiveReader:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._verified: dict[str, _VerifiedPartition] = {}
        self._verification_lock = threading.Lock()
        self._references: dict[str, tuple[tuple[int, int, int, int], HistoryReferenceIndex]] = {}

    def reference_index(self, snapshot: HistoryActiveSnapshot) -> HistoryReferenceIndex:
        try:
            source = SQLiteHistoryControlRepository(self._root / "control.sqlite3").read_source(
                snapshot.source_identity_hash
            )
        except HistoryControlError as exc:
            raise HistoryArchiveReadError("history_reference_unavailable") from exc
        if not source.supplier_contract.startswith("history_ref_"):
            raise HistoryArchiveReadError("history reference contract requires rebuilding history")
        digest = source.supplier_contract[len("history_ref_") : len("history_ref_") + 64]
        path = self._root / "references" / f"{digest}.json"
        try:
            stat = path.stat()
            identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self._references.get(digest)
            if cached is not None and cached[0] == identity:
                return cached[1]
            reference = read_history_reference(path)
            if reference.content_hash != digest:
                raise ValueError("history reference identity conflict")
            index = HistoryReferenceIndex(reference)
            self._references[digest] = (identity, index)
            return index
        except (OSError, ValueError) as exc:
            raise HistoryArchiveReadError("history_reference_unavailable") from exc

    def verify_snapshot(
        self,
        snapshot: HistoryActiveSnapshot,
        progress: Callable[[int, int, int, int, int, HistoryPartitionVerificationPhase], None] | None = None,
    ) -> None:
        total = len(snapshot.partitions)
        for completed, reference in enumerate(snapshot.partitions, start=1):
            self._verified_repository(
                reference,
                None if progress is None else _partition_progress(progress, completed, total),
            )
            if progress is not None:
                size = (self._root / reference.relative_path).stat().st_size
                progress(completed, total, completed, size, size, "row_count")

    def count_range(
        self,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
        *,
        codes: Collection[str],
    ) -> int:
        if start > end or end > snapshot.data_cutoff:
            raise ValueError("history range count is invalid")
        return sum(
            self._verified_repository(self._reference(snapshot, year, month)).count_range(
                start,
                end,
                snapshot_sequence=snapshot.sequence,
                codes=codes,
            )
            for year, month in route_history_months(start, end)
        )

    def row_count_upper_bound(
        self,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
    ) -> int:
        """Return the sealed row-count bound without opening partition files."""

        if start > end or end > snapshot.data_cutoff:
            raise ValueError("history range bound is invalid")
        return sum(self._reference(snapshot, year, month).row_count for year, month in route_history_months(start, end))

    def read_day(
        self,
        trade_date: date,
        snapshot: HistoryActiveSnapshot,
        *,
        board: str | None = None,
    ) -> tuple[HistoryRevision, ...]:
        if trade_date > snapshot.data_cutoff:
            raise ValueError("history day exceeds the snapshot cutoff")
        reference = self._reference(snapshot, trade_date.year, trade_date.month)
        repository = self._verified_repository(reference)
        index = self.reference_index(snapshot)
        return tuple(
            index.apply(row)
            for row in repository.read_day(trade_date, snapshot_sequence=snapshot.sequence, board=board)
        )

    def read_code_window(
        self,
        code: str,
        session_dates: tuple[date, ...],
        snapshot: HistoryActiveSnapshot,
    ) -> tuple[HistoryRevision, ...]:
        dates = tuple(session_dates)
        if _CODE.fullmatch(code) is None:
            raise ValueError("history code window identity is invalid")
        if (
            not dates
            or len(dates) > MAX_HISTORY_TRAINING_WINDOW_SESSIONS
            or dates != tuple(sorted(set(dates)))
            or dates[-1] > snapshot.data_cutoff
        ):
            raise ValueError("history code window dates are invalid")
        allowed = frozenset(dates)
        rows: list[HistoryRevision] = []
        for year, month in route_history_months(dates[0], dates[-1]):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            rows.extend(
                row
                for row in repository.read_code(
                    code,
                    dates[0],
                    dates[-1],
                    snapshot_sequence=snapshot.sequence,
                )
                if row.trade_date in allowed
            )
        index = self.reference_index(snapshot)
        return tuple(index.apply(row) for row in sorted(rows, key=lambda row: row.trade_date))

    def iter_snapshot_revisions(self, snapshot: HistoryActiveSnapshot) -> Iterator[HistoryRevision]:
        reference_index = self.reference_index(snapshot)
        for reference in snapshot.partitions:
            year, month = _reference_month(reference)
            repository = self._verified_repository(reference)
            end_day = calendar.monthrange(year, month)[1]
            yield from (
                reference_index.apply(row)
                for row in repository.iter_range(
                    date(year, month, 1),
                    date(year, month, end_day),
                    snapshot_sequence=snapshot.sequence,
                )
            )

    def iter_range(
        self,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
    ) -> Iterator[HistoryRevision]:
        """Stream only the months intersecting an inclusive date range."""

        if start > end or end > snapshot.data_cutoff:
            raise ValueError("history range scan is invalid")
        reference_index = self.reference_index(snapshot)
        yield from (reference_index.apply(row) for row in self._iter_price_revisions(start, end, snapshot))

    def _iter_price_revisions(
        self, start: date, end: date, snapshot: HistoryActiveSnapshot
    ) -> Iterator[HistoryRevision]:
        for year, month in route_history_months(start, end):
            repository = self._verified_repository(self._reference(snapshot, year, month))
            yield from repository.iter_range(start, end, snapshot_sequence=snapshot.sequence)

    def iter_range_by_code(
        self,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
    ) -> Iterator[HistoryRevision]:
        """Merge code/date ordered month streams without retaining a market-wide window."""

        if start > end or end > snapshot.data_cutoff:
            raise ValueError("history range scan is invalid")
        streams = tuple(
            self._verified_repository(self._reference(snapshot, year, month)).iter_range_by_code(
                start,
                end,
                snapshot_sequence=snapshot.sequence,
            )
            for year, month in route_history_months(start, end)
        )
        reference_index = self.reference_index(snapshot)
        yield from (
            reference_index.apply(row) for row in heapq.merge(*streams, key=lambda row: (row.code, row.trade_date))
        )

    def read_published_code_window(
        self,
        code: str,
        session_dates: tuple[date, ...],
        snapshot: HistoryActiveSnapshot,
    ) -> tuple[PublishedHistoryCell, ...]:
        if _CODE.fullmatch(code) is None:
            raise ValueError("published history code is invalid")
        dates = session_dates
        if (
            not dates
            or len(dates) > MAX_HISTORY_TRAINING_WINDOW_SESSIONS
            or dates != tuple(sorted(set(dates)))
            or dates[-1] > snapshot.data_cutoff
        ):
            raise ValueError("published history dates are invalid")
        allowed = frozenset(dates)
        rows: list[PublishedHistoryCell] = []
        for year, month in route_history_months(dates[0], dates[-1]):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            rows.extend(
                row
                for row in repository.iter_published_cells(
                    dates[0], dates[-1], snapshot_sequence=snapshot.sequence, code=code
                )
                if row.trade_date in allowed
            )
            self._verified_repository(reference)
        return tuple(rows)

    def iter_published_range_by_code(
        self, start: date, end: date, snapshot: HistoryActiveSnapshot
    ) -> Iterator[PublishedHistoryCell]:
        if start > end or end > snapshot.data_cutoff:
            raise ValueError("published history range is invalid")
        references = tuple(self._reference(snapshot, year, month) for year, month in route_history_months(start, end))
        streams = tuple(
            self._verified_repository(reference).iter_published_cells(start, end, snapshot_sequence=snapshot.sequence)
            for reference in references
        )
        yield from heapq.merge(*streams, key=lambda row: (row.code, row.trade_date))
        for reference in references:
            self._verified_repository(reference)

    def iter_shadow_facts(
        self,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
    ) -> Iterator[tuple[str, date, bool, bool]]:
        """Stream raw/qfq presence facts without materializing history revisions."""
        if start > end or end > snapshot.data_cutoff:
            raise ValueError("history shadow range is invalid")
        streams = tuple(
            self._shadow_repository(self._reference(snapshot, year, month)).iter_shadow_facts(
                start,
                end,
                snapshot_sequence=snapshot.sequence,
            )
            for year, month in route_history_months(start, end)
        )
        yield from heapq.merge(*streams, key=lambda row: (row[0], row[1]))

    def revised_dates(
        self,
        start: date,
        end: date,
        comparison: HistoryPartitionRevisionComparison,
    ) -> tuple[date, ...]:
        """Compare logical revision identities without trusting an obsolete file hash."""

        reference = comparison.physical_reference
        reference_month = _reference_month(reference)
        if start > end or (start.year, start.month) != reference_month or (end.year, end.month) != reference_month:
            raise ValueError("history partition revision comparison range is invalid")
        repository = self._verified_repository(reference)
        try:
            before = _revision_identities(
                repository.iter_range(start, end, snapshot_sequence=comparison.before_sequence)
            )
            after = _revision_identities(repository.iter_range(start, end, snapshot_sequence=comparison.after_sequence))
        except HistoryMonthPartitionError as exc:
            raise HistoryArchiveReadError("history partition revision comparison failed") from exc
        return tuple(
            sorted(day for day, code in set(before) | set(after) if before.get((day, code)) != after.get((day, code)))
        )

    def iter_code(
        self,
        code: str,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
    ) -> Iterator[HistoryRevision]:
        """Stream one code across only the months that cover its date range."""

        if _CODE.fullmatch(code) is None or start > end or end > snapshot.data_cutoff:
            raise ValueError("history code scan is invalid")
        index = self.reference_index(snapshot)
        for year, month in route_history_months(start, end):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            yield from (
                index.apply(row) for row in repository.read_code(code, start, end, snapshot_sequence=snapshot.sequence)
            )

    def iter_training_windows(
        self,
        snapshot: HistoryActiveSnapshot,
        calendar_dates: tuple[date, ...],
        codes: Collection[str],
        progress: Callable[[int], None] | None = None,
        *,
        window_sessions: int = HISTORY_TRAINING_WINDOW_SESSIONS,
    ) -> Iterator[HistoryTrainingWindow]:
        dates = tuple(calendar_dates)
        if (
            not dates
            or not HISTORY_TRAINING_WINDOW_SESSIONS <= window_sessions <= MAX_HISTORY_TRAINING_WINDOW_SESSIONS
            or len(dates) > BAOSTOCK_MAX_SESSIONS
            or dates != tuple(sorted(set(dates)))
            or dates[-1] > snapshot.data_cutoff
        ):
            raise ValueError("history training calendar is invalid")
        allowed_codes = _training_codes(codes)
        expected_months = route_history_months(dates[0], dates[-1])
        available_months = tuple(
            _reference_month(reference)
            for reference in snapshot.partitions
            if _reference_month(reference) in expected_months
        )
        if available_months != expected_months:
            raise HistoryArchiveReadError("history snapshot does not cover the active calendar")
        position = {day: index for index, day in enumerate(dates)}
        buffers: dict[str, deque[HistoryTrainingPoint]] = {}
        previous_positions: dict[str, int] = {}
        processed_rows = 0
        reference_index = self.reference_index(snapshot)
        current_revisions = (
            revision
            for revision in self._iter_price_revisions(dates[0], dates[-1], snapshot)
            if revision.code in allowed_codes
        )
        for revision in current_revisions:
            processed_rows += 1
            current_position = position.get(revision.trade_date)
            if current_position is None:
                raise HistoryArchiveReadError("history revision is outside the active calendar")
            buffer = buffers.setdefault(
                revision.code,
                deque(maxlen=window_sessions),
            )
            previous_position = previous_positions.get(revision.code)
            if previous_position is not None and current_position != previous_position + 1:
                buffer.clear()
            industry = reference_index.industry_on(revision.code, revision.trade_date)
            training_point = (
                revision.training_point if reference_index.eligible(revision.code) and industry is not None else None
            )
            if training_point is None:
                buffer.clear()
            else:
                buffer.append(training_point)
                if len(buffer) == window_sessions:
                    assert industry is not None
                    assert revision.cell.unadjusted is not None
                    yield HistoryTrainingWindow(
                        revision.code,
                        revision.board,
                        industry.industry,
                        False,
                        revision.cell.unadjusted.trading_status,
                        tuple(buffer),
                        window_sessions,
                    )
            previous_positions[revision.code] = current_position
            if progress is not None and processed_rows % 512 == 0:
                progress(processed_rows)
        if progress is not None:
            progress(processed_rows)

    def _reference(
        self,
        snapshot: HistoryActiveSnapshot,
        year: int,
        month: int,
    ) -> HistorySnapshotPartition:
        matches = tuple(item for item in snapshot.partitions if _reference_month(item) == (year, month))
        if len(matches) != 1:
            raise HistoryArchiveReadError("history snapshot month is missing")
        return matches[0]

    def _verified_repository(
        self,
        reference: HistorySnapshotPartition,
        progress: Callable[[int, int, HistoryPartitionVerificationPhase], None] | None = None,
    ) -> SQLiteHistoryMonthPartitionRepository:
        path = self._root / reference.relative_path
        with self._verification_lock:
            try:
                identity = _partition_file_identity(path)
                existing = self._verified.get(reference.relative_path)
                if existing is not None and existing.reference == reference and existing.identity == identity:
                    return existing.repository
                self._verified.pop(reference.relative_path, None)
                SQLiteHistoryMonthPartitionRepository.verify(path, reference, progress)
                if _partition_file_identity(path) != identity:
                    raise HistoryArchiveReadError("history snapshot partition changed during verification")
            except (HistoryArchiveReadError, HistoryMonthPartitionError, OSError) as exc:
                self._verified.pop(reference.relative_path, None)
                if isinstance(exc, HistoryArchiveReadError):
                    raise
                raise HistoryArchiveReadError("history snapshot partition verification failed") from exc
            year, month = _reference_month(reference)
            repository = SQLiteHistoryMonthPartitionRepository(path, year, month, immutable_read=True)
            self._verified[reference.relative_path] = _VerifiedPartition(reference, identity, repository)
            return repository

    def _shadow_repository(self, reference: HistorySnapshotPartition) -> SQLiteHistoryMonthPartitionRepository:
        """Open a read-only shadow reader without repeating sealed-file hashing."""
        year, month = _reference_month(reference)
        return SQLiteHistoryMonthPartitionRepository(
            self._root / reference.relative_path,
            year,
            month,
            immutable_read=True,
        )


def _training_codes(codes: Collection[str]) -> frozenset[str]:
    selected = frozenset(codes)
    if not selected or any(_CODE.fullmatch(code) is None for code in selected):
        raise ValueError("history training universe is invalid")
    return selected


def _reference_month(reference: HistorySnapshotPartition) -> tuple[int, int]:
    parts = Path(reference.relative_path).parts
    return int(parts[1]), int(Path(parts[2]).stem)


def _revision_identities(revisions: Iterator[HistoryRevision]) -> dict[tuple[date, str], str]:
    identities: dict[tuple[date, str], str] = {}
    for revision in revisions:
        key = (revision.trade_date, revision.code)
        if key in identities:
            raise HistoryArchiveReadError("history partition revision comparison contains duplicate rows")
        identities[key] = revision.revision_id
    return identities


def _partition_progress(
    progress: Callable[[int, int, int, int, int, HistoryPartitionVerificationPhase], None],
    current_partition: int,
    total_partitions: int,
) -> Callable[[int, int, HistoryPartitionVerificationPhase], None]:
    def report(completed_bytes: int, total_bytes: int, phase: HistoryPartitionVerificationPhase) -> None:
        progress(
            current_partition - 1,
            total_partitions,
            current_partition,
            completed_bytes,
            total_bytes,
            phase,
        )

    return report


__all__ = [
    "HistoryArchiveReadError",
    "HistoryPartitionRevisionComparison",
    "SQLiteHistoryArchiveReader",
    "route_history_months",
]
