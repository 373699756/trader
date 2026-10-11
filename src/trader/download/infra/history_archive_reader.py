"""Date-routed reader for an active set of monthly history partitions."""

from __future__ import annotations

import calendar
import heapq
import re
import threading
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal

from trader.download.domain.baostock_daily import BAOSTOCK_MAX_SESSIONS
from trader.download.domain.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.download.domain.history_revision import (
    MAX_HISTORY_TRAINING_WINDOW_SESSIONS,
    HistoryRevision,
    HistoryTrainingRow,
)
from trader.download.domain.published_history import PublishedHistoryCell
from trader.download.infra.history_control_repository import HistoryControlError, SQLiteHistoryControlRepository
from trader.download.infra.history_month_partition import (
    HistoryMonthPartitionError,
    HistoryPartitionVerificationPhase,
    SQLiteHistoryMonthPartitionRepository,
)
from trader.download.infra.history_reference_files import (
    HistoryReferenceIndex,
    history_industry_mapping_path,
    read_bound_history_reference,
)

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
    level: Literal["published_bytes", "full"]


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


def _with_training_industry(row: HistoryTrainingRow, index: HistoryReferenceIndex) -> HistoryTrainingRow:
    industry = index.industry_on(row.code, row.trade_date)
    return replace(row, industry=industry.industry if index.eligible(row.code) and industry is not None else None)


def _training_months(snapshot: HistoryActiveSnapshot, dates: tuple[date, ...]) -> tuple[tuple[int, int], ...]:
    if (
        not dates
        or len(dates) > BAOSTOCK_MAX_SESSIONS
        or dates != tuple(sorted(set(dates)))
        or dates[-1] > snapshot.data_cutoff
    ):
        raise ValueError("history training calendar is invalid")
    expected = route_history_months(dates[0], dates[-1])
    available = tuple(_reference_month(item) for item in snapshot.partitions if _reference_month(item) in expected)
    if available != expected:
        raise HistoryArchiveReadError("history snapshot does not cover the active calendar")
    return expected


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
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise HistoryArchiveReadError("history_reference_unavailable")
        path = history_industry_mapping_path(self._root)
        try:
            stat = path.stat()
            identity = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            cached = self._references.get(digest)
            if cached is not None and cached[0] == identity:
                return cached[1]
            reference = read_bound_history_reference(self._root, source.supplier_contract)
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
        self.reference_index(snapshot)
        total = len(snapshot.partitions)
        for completed, reference in enumerate(snapshot.partitions, start=1):
            self._repository_with_trust(
                reference,
                None if progress is None else _partition_progress(progress, completed, total),
                required_level="full",
                establish_level="full",
            )
            if progress is not None:
                size = (self._root / reference.relative_path).stat().st_size
                progress(completed, total, completed, size, size, "row_count")

    def verify_published_snapshot(
        self,
        snapshot: HistoryActiveSnapshot,
        progress: Callable[[int, int, int, int, int, HistoryPartitionVerificationPhase], None] | None = None,
    ) -> None:
        """Trust download-sealed SQLite facts after cross-process byte verification."""

        self.reference_index(snapshot)
        total = len(snapshot.partitions)
        for completed, reference in enumerate(snapshot.partitions, start=1):
            self._repository_with_trust(
                reference,
                None if progress is None else _partition_progress(progress, completed, total),
                required_level=None,
                establish_level="published_bytes",
            )
            if progress is not None:
                size = (self._root / reference.relative_path).stat().st_size
                progress(completed, total, completed, size, size, "hash")

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
        index = self.reference_index(snapshot)
        eligible_codes = tuple(code for code in codes if index.eligible(code))
        if not eligible_codes:
            return 0
        return sum(
            self._verified_repository(self._reference(snapshot, year, month)).count_range(
                start,
                end,
                snapshot_sequence=snapshot.sequence,
                codes=eligible_codes,
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
            if index.eligible(row.code)
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
        return tuple(
            index.apply(row) for row in sorted(rows, key=lambda row: row.trade_date) if index.eligible(row.code)
        )

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
                if reference_index.eligible(row.code)
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
        yield from (
            reference_index.apply(row)
            for row in self._iter_price_revisions(start, end, snapshot)
            if reference_index.eligible(row.code)
        )

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
            reference_index.apply(row)
            for row in heapq.merge(*streams, key=lambda row: (row.code, row.trade_date))
            if reference_index.eligible(row.code)
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
        if not self.reference_index(snapshot).eligible(code):
            return ()
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
        index = self.reference_index(snapshot)
        yield from (
            row for row in heapq.merge(*streams, key=lambda row: (row.code, row.trade_date)) if index.eligible(row.code)
        )
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
        if not index.eligible(code):
            return
        for year, month in route_history_months(start, end):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            yield from (
                index.apply(row) for row in repository.read_code(code, start, end, snapshot_sequence=snapshot.sequence)
            )

    def iter_training_rows(
        self,
        snapshot: HistoryActiveSnapshot,
        calendar_dates: tuple[date, ...],
        codes: Collection[str],
        progress: Callable[[int], None] | None = None,
        verification_progress: Callable[[int, int, int, int, int, HistoryPartitionVerificationPhase], None]
        | None = None,
    ) -> Iterator[HistoryTrainingRow]:
        """Stream only the typed fields consumed by training-owned rolling state."""

        dates = tuple(calendar_dates)
        allowed_codes = _training_codes(codes)
        expected_months = _training_months(snapshot, dates)
        allowed_dates = frozenset(dates)
        processed_rows = 0
        reference_index = self.reference_index(snapshot)
        total = len(snapshot.partitions)
        for completed, reference in enumerate(snapshot.partitions, start=1):
            repository = self._repository_with_trust(
                reference,
                None if verification_progress is None else _partition_progress(verification_progress, completed, total),
                required_level=None,
                establish_level="published_bytes",
                retain_pages=True,
            )
            try:
                if verification_progress is not None:
                    size = (self._root / reference.relative_path).stat().st_size
                    verification_progress(completed, total, completed, size, size, "hash")
                if _reference_month(reference) not in expected_months:
                    continue
                for row in repository.iter_training_rows(dates[0], dates[-1], snapshot_sequence=snapshot.sequence):
                    if row.code not in allowed_codes:
                        continue
                    processed_rows += 1
                    if row.trade_date not in allowed_dates:
                        raise HistoryArchiveReadError("history training row is outside the active calendar")
                    yield _with_training_industry(row, reference_index)
                    if progress is not None and processed_rows % 512 == 0:
                        progress(processed_rows)
                if (
                    _partition_file_identity(self._root / reference.relative_path)
                    != self._verified[reference.relative_path].identity
                ):
                    raise HistoryArchiveReadError("history snapshot partition changed during consumption")
            finally:
                repository.release_read_pages()
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
        return self._repository_with_trust(
            reference,
            progress,
            required_level=None,
            establish_level="full",
        )

    def _repository_with_trust(
        self,
        reference: HistorySnapshotPartition,
        progress: Callable[[int, int, HistoryPartitionVerificationPhase], None] | None,
        *,
        required_level: Literal["full"] | None,
        establish_level: Literal["published_bytes", "full"],
        retain_pages: bool = False,
    ) -> SQLiteHistoryMonthPartitionRepository:
        path = self._root / reference.relative_path
        with self._verification_lock:
            try:
                identity = _partition_file_identity(path)
                existing = self._verified.get(reference.relative_path)
                if (
                    existing is not None
                    and existing.reference == reference
                    and existing.identity == identity
                    and (required_level is None or existing.level == required_level)
                ):
                    return existing.repository
                self._verified.pop(reference.relative_path, None)
                if establish_level == "full":
                    SQLiteHistoryMonthPartitionRepository.verify(path, reference, progress)
                else:
                    SQLiteHistoryMonthPartitionRepository.verify_published_bytes(
                        path, reference, progress, retain_pages=retain_pages
                    )
                if _partition_file_identity(path) != identity:
                    raise HistoryArchiveReadError("history snapshot partition changed during verification")
            except (HistoryArchiveReadError, HistoryMonthPartitionError, OSError) as exc:
                self._verified.pop(reference.relative_path, None)
                if isinstance(exc, HistoryArchiveReadError):
                    raise
                raise HistoryArchiveReadError("history snapshot partition verification failed") from exc
            year, month = _reference_month(reference)
            repository = SQLiteHistoryMonthPartitionRepository(path, year, month, immutable_read=True)
            self._verified[reference.relative_path] = _VerifiedPartition(
                reference, identity, repository, establish_level
            )
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
