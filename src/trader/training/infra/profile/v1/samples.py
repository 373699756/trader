"""One bounded history scan into disposable, typed learned-rank sample matrices."""

from __future__ import annotations

import math
import mmap
import struct
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, Protocol, cast

import numpy as np

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnectionOptions,
    SqliteSchema,
    SqliteTransaction,
)
from trader.recommendation.domain.market.v1.features import (
    LEARNED_RANK_FEATURE_IDS,
    CalculateLearnedRankFeatures,
    LearnedRankDailyPoint,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.tomorrow_training import TomorrowTrainingProgress, TomorrowTrainingProgressPort
from trader.training.domain.label_benchmark import ObservedTrainingBenchmark
from trader.training.domain.v1.learned_rank import LearnedRankDateSplit
from trader.training.infra.history.aligned_training_rows import AlignmentReadDates, AlignTrainingRows
from trader.training.infra.history.history_training_input import HistoryTrainingInputSnapshot

_FEATURE_BYTES = struct.Struct(f"<{len(LEARNED_RANK_FEATURE_IDS)}d")


class LearnedRankInputPort(Protocol):
    @property
    def snapshot(self) -> HistoryTrainingInputSnapshot: ...

    def training_row_upper_bound(self, allowed_dates: frozenset[date]) -> int: ...

    def iter_training_rows(self, allowed_dates: frozenset[date]) -> Iterator[HistoryTrainingRow]: ...


def CheckV1Cancellation(cancel_requested: Callable[[], bool] | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise InterruptedError("learned_rank_training_cancelled")


@dataclass(frozen=True)
class LearnedRankSampleCounts:
    source_rows: int
    samples: int
    missing_prices: int
    missing_industry: int
    short_window: int
    missing_t1: int
    missing_d25: int
    suspended_sessions: int = 0
    estimated_sessions: int = 0


@dataclass(frozen=True)
class LearnedRankMatrix:
    features: np.ndarray
    targets: np.ndarray
    codes: tuple[str, ...]
    dates: tuple[date, ...]
    industries: tuple[str, ...]
    groups: tuple[int, ...]


@dataclass(frozen=True)
class LearnedRankPartitionCoverage:
    strategy: Strategy
    partition: Literal["fitting", "early_stopping", "calibration", "validation"]
    expected_dates: int
    effective_dates: int
    rows: int


@dataclass
class _Candidate:
    code: str
    industry: str
    features: tuple[float, ...]
    entry: float
    future: list[float | None] = field(default_factory=lambda: [None] * 5)


@dataclass
class _PendingDay:
    trade_date: date
    position: int
    candidates: dict[str, _Candidate] = field(default_factory=dict)


@dataclass
class _RollingCode:
    points: deque[LearnedRankDailyPoint] = field(default_factory=lambda: deque(maxlen=251))
    last_position: int = -1


class LearnedRankSampleIndex:
    """Own a disposable sample database; no production/history writes occur here."""

    def __init__(self, path: Path, *, read_only: bool = False) -> None:
        self._connection = OpenSqliteConnection(
            path,
            SqliteConnectionOptions(
                read_only=read_only,
                journal_mode=None if read_only else "OFF",
                durability="durable" if read_only else "rebuildable",
                cache_kib=16384,
            ),
        )
        if read_only:
            return
        try:
            EnsureSqliteSchema(
                self._connection,
                SqliteSchema(
                    None,
                    (
                        "CREATE TABLE samples (day TEXT NOT NULL, code TEXT NOT NULL, industry TEXT NOT NULL, "
                        "features BLOB NOT NULL, target_t1 REAL, target_d25 REAL, "
                        "PRIMARY KEY(day, code)) WITHOUT ROWID",
                    ),
                ),
                allow_create=True,
            )
        except BaseException:
            self._connection.close()
            raise

    def close(self) -> None:
        self._connection.close()

    def add_day(self, pending: _PendingDay) -> tuple[int, int, int]:
        candidates = tuple(pending.candidates.values())
        benchmarks = tuple(ObservedTrainingBenchmark(item.future[index] for item in candidates) for index in range(5))
        converted = missing_t1 = missing_d25 = 0
        with SqliteTransaction(self._connection):
            for item in candidates:
                excess = tuple(
                    value - benchmark if value is not None and benchmark is not None else None
                    for value, benchmark in zip(item.future, benchmarks, strict=True)
                )
                t1 = excess[0]
                d25 = (
                    math.fsum(cast(float, value) for value in excess[1:]) / 4.0
                    if all(value is not None for value in excess[1:])
                    else None
                )
                missing_t1 += t1 is None
                missing_d25 += d25 is None
                if t1 is None and d25 is None:
                    continue
                self._connection.run(
                    "INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        pending.trade_date.isoformat(),
                        item.code,
                        item.industry,
                        _FEATURE_BYTES.pack(*item.features),
                        t1,
                        d25,
                    ),
                )
                converted += 1
        return converted, missing_t1, missing_d25

    def coverage(
        self, splits: tuple[LearnedRankDateSplit, LearnedRankDateSplit]
    ) -> tuple[LearnedRankPartitionCoverage, ...]:
        counts = {
            date.fromisoformat(str(day)): (int(t1_rows), int(d25_rows))
            for day, t1_rows, d25_rows in self._connection.run(
                "SELECT day, COUNT(target_t1), COUNT(target_d25) FROM samples GROUP BY day"
            )
        }
        result: list[LearnedRankPartitionCoverage] = []
        for position, (strategy, split) in enumerate(zip((Strategy.TOMORROW, Strategy.D25), splits, strict=True)):
            partitions: tuple[
                tuple[Literal["fitting", "early_stopping", "calibration", "validation"], tuple[date, ...]], ...
            ] = (
                ("fitting", split.fitting),
                ("early_stopping", split.early_stopping),
                ("calibration", split.calibration),
                ("validation", split.validation),
            )
            for name, days in partitions:
                rows = tuple(counts.get(day, (0, 0))[position] for day in days)
                result.append(
                    LearnedRankPartitionCoverage(strategy, name, len(days), sum(value > 0 for value in rows), sum(rows))
                )
        return tuple(result)

    def matrix(
        self, dates: tuple[date, ...], strategy: Strategy, directory: Path, resources: ExitStack
    ) -> LearnedRankMatrix:
        column = "target_t1" if strategy is Strategy.TOMORROW else "target_d25"
        if strategy not in {Strategy.TOMORROW, Strategy.D25} or not dates:
            raise ValueError("learned-rank matrix requires one scored strategy and date partition")
        rows = int(
            self._connection.run(
                f"SELECT COUNT(*) FROM samples WHERE day BETWEEN ? AND ? AND {column} IS NOT NULL",
                (dates[0].isoformat(), dates[-1].isoformat()),
            ).require_one()[0]
        )
        if rows == 0:
            raise ValueError("learned_rank_sample_partition_empty")
        directory.mkdir(parents=True, exist_ok=True)
        buffers: list[mmap.mmap] = []
        for name, size in (("features", rows * _FEATURE_BYTES.size), ("targets", rows * 8)):
            with (directory / f"{name}.bin").open("w+b") as handle:
                handle.truncate(size)
                buffers.append(mmap.mmap(handle.fileno(), size))
                resources.callback(buffers[-1].close)
        features = np.ndarray((rows, len(LEARNED_RANK_FEATURE_IDS)), dtype=np.float64, buffer=buffers[0])
        targets = np.ndarray((rows,), dtype=np.float64, buffer=buffers[1])
        codes: list[str] = []
        sample_dates: list[date] = []
        industries: list[str] = []
        groups: list[int] = []
        allowed_dates = frozenset(dates)
        code_values: dict[str, str] = {}
        industry_values: dict[str, str] = {}
        date_values = {day.isoformat(): day for day in dates}
        with closing(
            self._connection.run(
                f"SELECT day, code, industry, features, {column} FROM samples "
                f"WHERE day BETWEEN ? AND ? AND {column} IS NOT NULL ORDER BY day, code",
                (dates[0].isoformat(), dates[-1].isoformat()),
            )
        ) as cursor:
            position = 0
            while batch := cursor.fetchmany(4_096):
                blobs: list[bytes] = []
                labels: list[float] = []
                for raw in batch:
                    day_text, code, industry, blob, target = cast(tuple[str, str, str, bytes, float], raw)
                    day = date_values.get(day_text)
                    if day not in allowed_dates:
                        raise ValueError("learned-rank sample crossed a date partition")
                    assert day is not None
                    if len(blob) != _FEATURE_BYTES.size:
                        raise struct.error(f"unpack requires a buffer of {_FEATURE_BYTES.size} bytes")
                    blobs.append(blob)
                    labels.append(target)
                    if not sample_dates or day != sample_dates[-1]:
                        groups.append(0)
                    groups[-1] += 1
                    codes.append(code_values.setdefault(code, code))
                    sample_dates.append(day)
                    industries.append(industry_values.setdefault(industry, industry))
                end = position + len(batch)
                features[position:end] = np.frombuffer(b"".join(blobs), dtype="<f8").reshape(
                    len(batch), len(LEARNED_RANK_FEATURE_IDS)
                )
                targets[position:end] = labels
                position = end
        return LearnedRankMatrix(features, targets, tuple(codes), tuple(sample_dates), tuple(industries), tuple(groups))


@dataclass(frozen=True)
class LearnedRankSampleStream:
    progress: TomorrowTrainingProgressPort | None = None
    cancel_requested: Callable[[], bool] | None = None
    aligned_rows: Iterator[HistoryTrainingRow] | None = None


_STANDALONE_STREAM = LearnedRankSampleStream()


def BuildLearnedRankSamples(
    source: LearnedRankInputPort,
    splits: tuple[LearnedRankDateSplit, LearnedRankDateSplit],
    index: LearnedRankSampleIndex,
    *,
    stream: LearnedRankSampleStream = _STANDALONE_STREAM,
) -> LearnedRankSampleCounts:
    progress, cancel_requested, aligned_rows = stream.progress, stream.cancel_requested, stream.aligned_rows
    calendar = source.snapshot.calendar.open_dates
    positions = {day: position for position, day in enumerate(calendar)}
    selected = frozenset(day for split in splits for day in split.sample_dates)
    first_position = min(positions[day] for day in selected) - 251
    readable = frozenset(calendar[first_position:])
    readable = AlignmentReadDates(calendar, readable)
    total = source.training_row_upper_bound(readable)
    rolling: dict[str, _RollingCode] = {}
    pending: deque[_PendingDay] = deque()
    current: date | None = None
    source_rows = converted = missing_prices = missing_industry = short_window = missing_t1 = missing_d25 = 0
    suspended_sessions = estimated_sessions = 0
    last_key: tuple[date, str] | None = None
    rows = (
        aligned_rows
        if aligned_rows is not None
        else AlignTrainingRows(
            source.iter_training_rows(readable), tuple(sorted(readable)), source.snapshot.listing_intervals
        )
    )
    for row in rows:
        source_rows += 1
        if source_rows % 512 == 0:
            CheckV1Cancellation(cancel_requested)
            if progress is not None:
                progress.publish(
                    TomorrowTrainingProgress(
                        "history_conversion", "running", source_rows, max(source_rows, total), converted
                    )
                )
        key = (row.trade_date, row.code)
        if last_key is not None and key <= last_key:
            raise ValueError("learned-rank history must be unique and ordered by date/code")
        last_key = key
        position = positions[row.trade_date]
        if row.trade_date != current:
            while pending and position - pending[0].position > 5:
                added, absent_t1, absent_d25 = index.add_day(pending.popleft())
                converted += added
                missing_t1 += absent_t1
                missing_d25 += absent_d25
            current = row.trade_date
            if row.trade_date in selected:
                pending.append(_PendingDay(row.trade_date, position))
        state = rolling.setdefault(row.code, _RollingCode())
        if state.last_position != position - 1:
            state.points.clear()
        point = _complete_point(row)
        if point is not None:
            suspended_sessions += row.cell_status == "supplier_marked_suspended"
            estimated_sessions += row.cell_status in {"unknown_missing", "tencent_inferred_suspended"}
        if point is None:
            missing_prices += 1
            state.points.clear()
            state.last_position = position
            continue
        for day in pending:
            candidate = day.candidates.get(row.code)
            horizon = position - day.position
            if (
                candidate is not None
                and 1 <= horizon <= 5
                and row.cell_status in {None, "complete", "supplier_marked_suspended"}
            ):
                candidate.future[horizon - 1] = point.close_price / candidate.entry - 1.0
        if row.trade_date in selected and row.cell_status in {None, "complete"} and point.amount > 0.0:
            if row.industry is None:
                missing_industry += 1
            elif (features := CalculateLearnedRankFeatures(tuple(state.points))) is None:
                short_window += 1
            else:
                pending[-1].candidates[row.code] = _Candidate(row.code, row.industry, features, point.close_price)
        state.points.append(point)
        state.last_position = position
    while pending:
        added, absent_t1, absent_d25 = index.add_day(pending.popleft())
        converted += added
        missing_t1 += absent_t1
        missing_d25 += absent_d25
    CheckV1Cancellation(cancel_requested)
    return LearnedRankSampleCounts(
        source_rows,
        converted,
        missing_prices,
        missing_industry,
        short_window,
        missing_t1,
        missing_d25,
        suspended_sessions,
        estimated_sessions,
    )


def _complete_point(row: HistoryTrainingRow) -> LearnedRankDailyPoint | None:
    point = row.point
    if (
        point is None
        or point.qfq_close_price is None
        or point.qfq_amount is None
        or point.qfq_close_price <= 0.0
        or point.qfq_amount < 0.0
    ):
        return None
    return LearnedRankDailyPoint(row.trade_date, point.qfq_close_price, point.qfq_amount)


__all__ = [
    "BuildLearnedRankSamples",
    "CheckV1Cancellation",
    "LearnedRankMatrix",
    "LearnedRankInputPort",
    "LearnedRankPartitionCoverage",
    "LearnedRankSampleCounts",
    "LearnedRankSampleIndex",
]
