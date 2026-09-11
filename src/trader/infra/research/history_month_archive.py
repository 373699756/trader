"""Date-routed reader for an active set of monthly history partitions."""

from __future__ import annotations

import calendar
import re
from collections import deque
from collections.abc import Callable, Collection, Iterator
from datetime import date
from pathlib import Path

from trader.domain.research.baostock_daily import BAOSTOCK_MAX_SESSIONS, BaoStockTrainingRow
from trader.domain.research.history_control import HistoryActiveSnapshot, HistorySnapshotPartition
from trader.domain.research.history_monthly import (
    HISTORY_TRAINING_WINDOW_SESSIONS,
    HistoryMonthlyRevision,
    HistoryTrainingWindow,
)
from trader.infra.research.history_month_partition import (
    HistoryMonthPartitionError,
    SQLiteHistoryMonthPartitionRepository,
)

_CODE = re.compile(r"^[0-9]{6}$")


class HistoryMonthlyArchiveError(RuntimeError):
    """The active monthly history view is incomplete or inconsistent."""


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


class SQLiteHistoryMonthlyArchive:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._verified: dict[HistorySnapshotPartition, SQLiteHistoryMonthPartitionRepository] = {}

    def verify_snapshot(
        self,
        snapshot: HistoryActiveSnapshot,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        total = len(snapshot.partitions)
        for completed, reference in enumerate(snapshot.partitions, start=1):
            self._verified_repository(reference)
            if progress is not None:
                progress(completed, total)

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

    def read_day(
        self,
        trade_date: date,
        snapshot: HistoryActiveSnapshot,
        *,
        board: str | None = None,
    ) -> tuple[HistoryMonthlyRevision, ...]:
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
    ) -> tuple[HistoryMonthlyRevision, ...]:
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
        rows: list[HistoryMonthlyRevision] = []
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

    def iter_snapshot_revisions(self, snapshot: HistoryActiveSnapshot) -> Iterator[HistoryMonthlyRevision]:
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
    ) -> Iterator[HistoryMonthlyRevision]:
        """Stream only the months intersecting an inclusive date range."""

        if start > end or end > snapshot.data_cutoff:
            raise ValueError("history range scan is invalid")
        for year, month in route_history_months(start, end):
            reference = self._reference(snapshot, year, month)
            repository = self._verified_repository(reference)
            yield from repository.iter_range(start, end, snapshot_sequence=snapshot.sequence)

    def iter_code(
        self,
        code: str,
        start: date,
        end: date,
        snapshot: HistoryActiveSnapshot,
    ) -> Iterator[HistoryMonthlyRevision]:
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
    ) -> Iterator[HistoryTrainingWindow]:
        dates = tuple(calendar_dates)
        if (
            not dates
            or len(dates) > BAOSTOCK_MAX_SESSIONS
            or dates != tuple(sorted(set(dates)))
            or dates[-1] != snapshot.data_cutoff
        ):
            raise ValueError("history training calendar is invalid")
        expected_months = route_history_months(dates[0], dates[-1])
        if tuple(_reference_month(reference) for reference in snapshot.partitions) != expected_months:
            raise HistoryMonthlyArchiveError("history snapshot does not cover the active calendar")
        position = {day: index for index, day in enumerate(dates)}
        buffers: dict[str, deque[BaoStockTrainingRow]] = {}
        previous_positions: dict[str, int] = {}
        for revision in self.iter_snapshot_revisions(snapshot):
            current_position = position.get(revision.trade_date)
            if current_position is None:
                raise HistoryMonthlyArchiveError("history revision is outside the active calendar")
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

    def _reference(
        self,
        snapshot: HistoryActiveSnapshot,
        year: int,
        month: int,
    ) -> HistorySnapshotPartition:
        matches = tuple(item for item in snapshot.partitions if _reference_month(item) == (year, month))
        if len(matches) != 1:
            raise HistoryMonthlyArchiveError("history snapshot month is missing")
        return matches[0]

    def _verified_repository(
        self,
        reference: HistorySnapshotPartition,
    ) -> SQLiteHistoryMonthPartitionRepository:
        existing = self._verified.get(reference)
        if existing is not None:
            return existing
        year, month = _reference_month(reference)
        path = self._root / reference.relative_path
        try:
            SQLiteHistoryMonthPartitionRepository.verify(path, reference)
        except HistoryMonthPartitionError as exc:
            raise HistoryMonthlyArchiveError("history snapshot partition verification failed") from exc
        repository = SQLiteHistoryMonthPartitionRepository(path, year, month)
        self._verified[reference] = repository
        return repository


def _reference_month(reference: HistorySnapshotPartition) -> tuple[int, int]:
    parts = Path(reference.relative_path).parts
    return int(parts[1]), int(Path(parts[2]).stem)


__all__ = ["HistoryMonthlyArchiveError", "SQLiteHistoryMonthlyArchive", "route_history_months"]
