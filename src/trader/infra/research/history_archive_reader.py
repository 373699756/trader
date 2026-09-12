"""Date-routed reader for an active set of monthly history partitions."""

from __future__ import annotations

import calendar
import re
from collections import deque
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.domain.research.baostock_daily import BAOSTOCK_MAX_SESSIONS, BaoStockTrainingRow
from trader.domain.research.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.domain.research.history_revision import (
    HISTORY_TRAINING_WINDOW_SESSIONS,
    HistoryRevision,
    HistoryTrainingWindow,
)
from trader.infra.research.history_month_partition import (
    HistoryMonthPartitionError,
    HistoryPartitionVerificationPhase,
    SQLiteHistoryMonthPartitionRepository,
)

_CODE = re.compile(r"^[0-9]{6}$")


class HistoryArchiveReadError(RuntimeError):
    """The active monthly history view is incomplete or inconsistent."""


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
        self._verified: dict[HistorySnapshotPartition, SQLiteHistoryMonthPartitionRepository] = {}

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
        return repository.read_day(trade_date, snapshot_sequence=snapshot.sequence, board=board)

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
            or len(dates) > HISTORY_TRAINING_WINDOW_SESSIONS
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
        return tuple(sorted(rows, key=lambda row: row.trade_date))

    def iter_snapshot_revisions(self, snapshot: HistoryActiveSnapshot) -> Iterator[HistoryRevision]:
        for reference in snapshot.partitions:
            year, month = _reference_month(reference)
            repository = self._verified_repository(reference)
            end_day = calendar.monthrange(year, month)[1]
            yield from repository.iter_range(
                date(year, month, 1),
                date(year, month, end_day),
                snapshot_sequence=snapshot.sequence,
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
        for year, month in route_history_months(start, end):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            yield from repository.iter_range(start, end, snapshot_sequence=snapshot.sequence)

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
        for year, month in route_history_months(start, end):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            yield from repository.read_code(code, start, end, snapshot_sequence=snapshot.sequence)

    def iter_training_windows(
        self,
        snapshot: HistoryActiveSnapshot,
        calendar_dates: tuple[date, ...],
        progress: Callable[[int], None] | None = None,
    ) -> Iterator[HistoryTrainingWindow]:
        dates = tuple(calendar_dates)
        if (
            not dates
            or len(dates) > BAOSTOCK_MAX_SESSIONS
            or dates != tuple(sorted(set(dates)))
            or dates[-1] > snapshot.data_cutoff
        ):
            raise ValueError("history training calendar is invalid")
        expected_months = route_history_months(dates[0], dates[-1])
        available_months = tuple(
            _reference_month(reference)
            for reference in snapshot.partitions
            if _reference_month(reference) in expected_months
        )
        if available_months != expected_months:
            raise HistoryArchiveReadError("history snapshot does not cover the active calendar")
        position = {day: index for index, day in enumerate(dates)}
        buffers: dict[str, deque[BaoStockTrainingRow]] = {}
        previous_positions: dict[str, int] = {}
        processed_rows = 0
        for revision in self.iter_range(dates[0], dates[-1], snapshot):
            processed_rows += 1
            current_position = position.get(revision.trade_date)
            if current_position is None:
                raise HistoryArchiveReadError("history revision is outside the active calendar")
            buffer = buffers.setdefault(
                revision.code,
                deque(maxlen=HISTORY_TRAINING_WINDOW_SESSIONS),
            )
            previous_position = previous_positions.get(revision.code)
            if previous_position is not None and current_position != previous_position + 1:
                buffer.clear()
            training_row = revision.training_row
            if training_row is None:
                buffer.clear()
            else:
                buffer.append(training_row)
                if len(buffer) == HISTORY_TRAINING_WINDOW_SESSIONS:
                    yield HistoryTrainingWindow(tuple(buffer))
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
        existing = self._verified.get(reference)
        if existing is not None:
            return existing
        year, month = _reference_month(reference)
        path = self._root / reference.relative_path
        try:
            SQLiteHistoryMonthPartitionRepository.verify(path, reference, progress)
        except HistoryMonthPartitionError as exc:
            raise HistoryArchiveReadError("history snapshot partition verification failed") from exc
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        self._verified[reference] = repository
        return repository


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
