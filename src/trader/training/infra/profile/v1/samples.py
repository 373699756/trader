"""One bounded history scan into disposable, typed learned-rank sample matrices."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import math
import mmap
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing
from dataclasses import dataclass, field
from datetime import date
from itertools import repeat
from pathlib import Path
from typing import Literal, Protocol, cast

import numpy as np

from trader.download.domain.history_revision import HistoryTrainingRow
from trader.infra.sqlite_codec import DecodeDate, EncodeDate
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
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
from trader.training.infra.sample_schema import SampleFeatureHash

LEARNED_RANK_SAMPLE_COLUMNS = (
    "return_1d", "return_3d", "return_5d", "return_20d", "return_60d",
    "return_120d", "return_250d", "volatility_20d", "drawdown_60d", "log_avg_amount_20d",
)
_SAMPLE_SCHEMA = SqliteSchema(
    2,
    (
        "CREATE TABLE sample_contract (singleton INTEGER PRIMARY KEY CHECK(singleton=1),"
        "feature_hash BLOB NOT NULL CHECK(length(feature_hash)=32))",
        "CREATE TABLE samples (trade_date INTEGER NOT NULL,code TEXT NOT NULL,industry TEXT NOT NULL,"
        + ",".join(f"{name} REAL NOT NULL" for name in LEARNED_RANK_SAMPLE_COLUMNS)
        + ",zero_signs INTEGER NOT NULL CHECK(zero_signs BETWEEN 0 AND 1023),"
        "target_t1 REAL,target_d25 REAL,PRIMARY KEY(trade_date,code)) WITHOUT ROWID",
    ),
)


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
class LearnedRankMatrixRequest:
    dates: tuple[date, ...]
    strategy: Strategy

    def __post_init__(self) -> None:
        if (
            self.strategy not in {Strategy.TOMORROW, Strategy.D25}
            or not self.dates or self.dates != tuple(sorted(set(self.dates)))
        ):
            raise ValueError("learned-rank matrix requires one scored strategy and date partition")


@dataclass
class _MatrixOutput:
    features: np.ndarray
    targets: np.ndarray
    codes: list[str] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    groups: list[int] = field(default_factory=list)


def _empty_output(rows: int, directory: Path, resources: ExitStack) -> _MatrixOutput:
    directory.mkdir(parents=True, exist_ok=True)
    arrays = []
    for name, shape in (("features", (rows, len(LEARNED_RANK_SAMPLE_COLUMNS))), ("targets", (rows,))):
        with (directory / f"{name}.bin").open("w+b") as handle:
            handle.truncate(math.prod(shape) * 8)
            buffer = mmap.mmap(handle.fileno(), math.prod(shape) * 8)
        resources.callback(buffer.close)
        arrays.append(np.ndarray(shape, dtype=np.float64, buffer=buffer))
    return _MatrixOutput(arrays[0], arrays[1])


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
    points: deque[LearnedRankDailyPoint]
    last_position: int = -1


class LearnedRankSampleIndex:
    """Own a disposable sample database; no production/history writes occur here."""

    def __init__(self, path: Path, *, read_only: bool = False, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self._connection = OpenSqliteConnection(
            path,
            SqliteConnectionOptions(
                read_only=read_only,
                journal_mode=None if read_only else "OFF",
                durability="durable" if read_only else "rebuildable",
                cache_kib=16384,
            ), sqlite_policy=self._sqlite_policy,
        )
        try:
            EnsureSqliteSchema(
                self._connection,
                _SAMPLE_SCHEMA,
                allow_create=not read_only,
                initialize=self._initialize_contract,
                validate=self._validate_contract,
            )
        except BaseException:
            self._connection.close()
            raise

    @staticmethod
    def _initialize_contract(connection: SqliteConnection) -> None:
        connection.run("INSERT INTO sample_contract VALUES(1,?)", (SampleFeatureHash(LEARNED_RANK_FEATURE_IDS, None),))

    @staticmethod
    def _validate_contract(connection: SqliteConnection) -> None:
        row = connection.run("SELECT feature_hash FROM sample_contract WHERE singleton=1").require_one()
        if row[0] != SampleFeatureHash(LEARNED_RANK_FEATURE_IDS, None):
            raise ValueError("learned_rank_sample_feature_contract_mismatch")

    def close(self) -> None:
        self._connection.close()

    def add_day(self, pending: _PendingDay) -> tuple[int, int, int]:
        candidates = tuple(pending.candidates.values())
        benchmarks = tuple(ObservedTrainingBenchmark(item.future[index] for item in candidates) for index in range(5))
        converted = missing_t1 = missing_d25 = 0
        rows: list[tuple[object, ...]] = []
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
                if len(item.features) != len(LEARNED_RANK_SAMPLE_COLUMNS) or not all(map(math.isfinite, item.features)):
                    raise ValueError("learned_rank_sample_features_invalid")
                flags = sum(
                    1 << position for position, value in enumerate(item.features)
                    if value == 0.0 and math.copysign(1.0, value) < 0
                )
                rows.append((EncodeDate(pending.trade_date), item.code, item.industry, *item.features, flags, t1, d25))
                converted += 1
            self._connection.run_many(
                "INSERT INTO samples VALUES (" + ",".join("?" for _ in range(16)) + ")", rows
            )
        return converted, missing_t1, missing_d25

    def coverage(
        self, splits: tuple[LearnedRankDateSplit, LearnedRankDateSplit]
    ) -> tuple[LearnedRankPartitionCoverage, ...]:
        counts = {
            DecodeDate(day): (int(t1_rows), int(d25_rows))
            for day, t1_rows, d25_rows in self._connection.run(
                "SELECT trade_date, COUNT(target_t1), COUNT(target_d25) FROM samples GROUP BY trade_date"
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

    def read_matrices(
        self,
        requests: tuple[LearnedRankMatrixRequest, ...],
        directory: Path,
        resources: ExitStack,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[LearnedRankMatrix, ...]:
        """Decode one ordered SQLite stream into independent head/partition matrices."""
        if not requests or len(requests) > 8:
            raise ValueError("learned_rank_matrix_requests_invalid")
        dates_by_request = tuple(frozenset(request.dates) for request in requests)
        date_values = {EncodeDate(day): day for request in requests for day in request.dates}
        start, end = min(date_values), max(date_values)
        counts = tuple(
            (DecodeDate(row[0]), int(row[1]), int(row[2])) for row in self._connection.run(
                "SELECT trade_date,COUNT(target_t1),COUNT(target_d25) FROM samples "
                "WHERE trade_date BETWEEN ? AND ? GROUP BY trade_date", (start, end),
            )
        )
        outputs = []
        for number, request in enumerate(requests):
            target = 1 if request.strategy is Strategy.TOMORROW else 2
            if any(request.dates[0] <= row[0] <= request.dates[-1]
                   and row[0] not in dates_by_request[number] and cast(int, row[target]) > 0 for row in counts):
                raise ValueError("learned-rank sample crossed a date partition")
            rows = sum(cast(int, row[target]) for row in counts if row[0] in dates_by_request[number])
            if rows == 0:
                raise ValueError("learned_rank_sample_partition_empty")
            outputs.append(_empty_output(rows, directory / str(number), resources))
        code_values: dict[str, str] = {}
        industry_values: dict[str, str] = {}
        width = len(LEARNED_RANK_SAMPLE_COLUMNS)
        with closing(self._connection.run(
            f"SELECT trade_date,code,industry,{','.join(LEARNED_RANK_SAMPLE_COLUMNS)},"
            "zero_signs,target_t1,target_d25 FROM samples WHERE trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date,code", (start, end),
        )) as cursor:
            while batch := cursor.fetch_values(4096):
                CheckV1Cancellation(cancel_requested)
                columns = tuple(zip(*batch, strict=True))
                values = np.asarray(columns[3:], dtype=np.float64).T
                flags = values[:, width].astype(np.int64)
                if (
                    not np.isfinite(values[:, :width + 1]).all()
                    or np.isinf(values[:, width + 1:]).any()
                    or np.any((flags < 0) | (flags > 1023) | (flags != values[:, width]))
                ):
                    raise ValueError("learned_rank_sample_features_invalid")
                for position in range(width):
                    negative = (flags & (1 << position)) != 0
                    if np.any(values[negative, position] != 0.0):
                        raise ValueError("learned_rank_sample_zero_signs_invalid")
                    values[negative, position] = -0.0
                day_numbers = np.asarray(columns[0], dtype=np.int64)
                batch_codes = cast(tuple[str, ...], columns[1])
                batch_industries = cast(tuple[str, ...], columns[2])
                codes = np.asarray(tuple(map(code_values.setdefault, batch_codes, batch_codes)), dtype=object)
                industries = np.asarray(tuple(map(industry_values.setdefault, batch_industries, batch_industries)), dtype=object)
                for request, output in zip(requests, outputs, strict=True):
                    target = width + (1 if request.strategy is Strategy.TOMORROW else 2)
                    selected = np.flatnonzero(
                        (day_numbers >= EncodeDate(request.dates[0]))
                        & (day_numbers <= EncodeDate(request.dates[-1]))
                        & ~np.isnan(values[:, target])
                    )
                    if not len(selected):
                        continue
                    first = len(output.codes)
                    last = first + len(selected)
                    output.features[first:last] = values[selected, :width]
                    output.targets[first:last] = values[selected, target]
                    output.codes.extend(codes[selected].tolist())
                    output.industries.extend(industries[selected].tolist())
                    days, group_counts = np.unique(day_numbers[selected], return_counts=True)
                    for day_number, group_count in zip(days, group_counts, strict=True):
                        day = date_values[int(day_number)]
                        count = int(group_count)
                        if not output.dates or output.dates[-1] != day:
                            output.groups.append(0)
                        output.groups[-1] += count
                        output.dates.extend(repeat(day, count))
        if any(len(output.codes) != len(output.targets) for output in outputs):
            raise RuntimeError("learned_rank_sample_count_changed")
        return tuple(
            LearnedRankMatrix(
                output.features, output.targets, tuple(output.codes), tuple(output.dates),
                tuple(output.industries), tuple(output.groups),
            ) for output in outputs
        )


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
    history_sessions: int,
    stream: LearnedRankSampleStream = _STANDALONE_STREAM,
) -> LearnedRankSampleCounts:
    progress, cancel_requested, aligned_rows = stream.progress, stream.cancel_requested, stream.aligned_rows
    calendar = source.snapshot.calendar.open_dates
    positions = {day: position for position, day in enumerate(calendar)}
    selected = frozenset(day for split in splits for day in split.sample_dates)
    if history_sessions < 1:
        raise ValueError("learned-rank history sessions must be positive")
    first_position = min(positions[day] for day in selected) - history_sessions
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
        state = rolling.setdefault(row.code, _RollingCode(deque(maxlen=history_sessions)))
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
    "LEARNED_RANK_SAMPLE_COLUMNS",
    "BuildLearnedRankSamples",
    "CheckV1Cancellation",
    "LearnedRankMatrix",
    "LearnedRankMatrixRequest",
    "LearnedRankInputPort",
    "LearnedRankPartitionCoverage",
    "LearnedRankSampleCounts",
    "LearnedRankSampleIndex",
]
