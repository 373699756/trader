"""SQLite repository for disposable profile-scoped multi-target samples."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import math
import mmap
from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, cast

import numpy as np

from trader.infra.sqlite_codec import DecodeDate, EncodeDate, EncodeHash
from trader.infra.sqlite_connection import (
    EnsureSqliteSchema,
    OpenSqliteConnection,
    SqliteConnection,
    SqliteConnectionOptions,
    SqliteTransaction,
)
from trader.training.domain.training_dates import TrainingDateSplit
from trader.training.infra.model_bundles.contracts import TrainedHeadContract
from trader.training.infra.sample_sequences import SampleSequenceLayout, SampleSequenceReader, SampleSequenceWriter
from trader.training.infra.sample_schema import SAMPLE_TARGET_COLUMNS, SampleFeatureHash, TrainingSampleSchema

TrainingSplitName = Literal["training", "early_stopping", "calibration", "validation", "holdout"]
TRAINING_SAMPLE_CACHE_MIB = 32
TRAINING_SAMPLE_MMAP_BYTES = 0
TRAINING_MATRIX_BYTES = 256 * 1024**2


@dataclass(frozen=True)
class TrainingSample:
    code: str
    trade_date: date
    board: str
    industry: str
    average_amount_20d: float
    features: tuple[float, ...]
    targets: tuple[float | None, float | None, float | None, float | None, float | None, float | None]


@dataclass(frozen=True)
class TrainingSampleMatrix:
    features: np.ndarray
    labels: np.ndarray


@dataclass(frozen=True)
class TrainingSampleBatch(TrainingSampleMatrix):
    trade_dates: tuple[date, ...]
    codes: tuple[str, ...]
    industries: tuple[str, ...]


@dataclass(frozen=True)
class TrainingIndustryCounts:
    industry: str
    training: int
    early_stopping: int
    calibration: int
    validation: int
    calibration_dates: int = 0
    holdout: int = 0


@dataclass(frozen=True)
class TrainingIndustryData:
    training: TrainingSampleMatrix
    early_stopping: TrainingSampleMatrix
    calibration: TrainingSampleMatrix
    validation_count: int
    calibration_dates: tuple[date, ...] = ()


@dataclass(frozen=True)
class _IndustryRows:
    industry: str
    features: np.ndarray
    targets: np.ndarray
    dates: tuple[date, ...]
    codes: tuple[str, ...]
    splits: tuple[str, ...]


@dataclass(frozen=True)
class TargetMetric:
    target: str
    count: int
    mean: float | None
    standard_deviation: float | None


@dataclass(frozen=True)
class SampleDateGap:
    trade_date: date
    sample_rows: int
    candidates: int
    missing_future_returns: tuple[int, ...]


@dataclass(frozen=True)
class SampleCoverage:
    target: str
    partition: TrainingSplitName
    expected_dates: int
    effective_dates: int
    sample_rows: int
    target_rows: int
    missing: tuple[SampleDateGap, ...]


class SQLiteTrainingSampleIndex:
    """Own one temporary SQLite database shared by both sequential fits."""

    def __init__(
        self,
        path: Path,
        feature_names: tuple[str, ...],
        *,
        prepared_split_hash: str | None = None,
        sequence_layout: SampleSequenceLayout | None = None, sqlite_policy: SqliteIoPolicy,
    ) -> None:
        self._sqlite_policy = sqlite_policy
        self._feature_hash = SampleFeatureHash(feature_names, sequence_layout)
        self._path = path
        self._feature_names = feature_names
        self._feature_count = len(feature_names)
        self._sequence_layout = sequence_layout
        self._column_features = sequence_layout.auxiliary if sequence_layout else len(feature_names)
        self._prepared_split_hash = prepared_split_hash
        self._counts_by_target: dict[str, tuple[TrainingIndustryCounts, ...]] = {}
        self._date_values: dict[int, date] = {}
        self._shared_rows: _IndustryRows | None = None
        self._connection = OpenSqliteConnection(
            path,
            SqliteConnectionOptions(
                read_only=prepared_split_hash is not None,
                journal_mode="OFF" if prepared_split_hash is None else None,
                durability="rebuildable" if prepared_split_hash is None else "durable",
                cache_kib=TRAINING_SAMPLE_CACHE_MIB * 1024,
                mmap_bytes=TRAINING_SAMPLE_MMAP_BYTES,
                temp_storage="FILE",
            ), sqlite_policy=self._sqlite_policy,
        )
        self._transaction = SqliteTransaction(self._connection, immediate=True)
        self._sequence_writer = SampleSequenceWriter(self._connection, sequence_layout) if sequence_layout else None
        try:
            self._initialize()
        except BaseException:
            self._connection.close()
            raise

    def _initialize(self) -> None:
        EnsureSqliteSchema(
            self._connection,
            TrainingSampleSchema(self._feature_names, self._sequence_layout),
            allow_create=self._prepared_split_hash is None,
            initialize=self._initialize_contract,
            validate=self._validate_contract,
        )
        if self._prepared_split_hash is None:
            self._transaction.begin()

    def _initialize_contract(self, connection: SqliteConnection) -> None:
        connection.run("INSERT INTO sample_contract VALUES(1,2,?,NULL)", (self._feature_hash,))

    def _validate_contract(self, connection: SqliteConnection) -> None:
        row = connection.run("SELECT schema_version,feature_hash,split_hash FROM sample_contract WHERE singleton=1").require_one()
        if row[0] != 2 or row[1] != self._feature_hash:
            raise ValueError("training_sample_feature_contract_mismatch")
        expected = EncodeHash(self._prepared_split_hash) if self._prepared_split_hash is not None else None
        if row[2] != expected:
            raise ValueError("training_sample_split_contract_mismatch")

    def _feature_columns(self, contract: TrainedHeadContract) -> tuple[str, ...]:
        if not contract.feature_positions or any(
            position < 0 or position >= self._feature_count for position in contract.feature_positions
        ):
            raise ValueError("training_sample_feature_contract_mismatch")
        names = tuple(self._feature_names[position] for position in contract.feature_positions)
        if names != contract.feature_manifest.names:
            raise ValueError("training_sample_feature_contract_mismatch")
        return names

    def close(self) -> None:
        try:
            self._transaction.rollback()
        finally:
            self._connection.close()

    def __enter__(self) -> SQLiteTrainingSampleIndex:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def add_final(self, samples: Iterable[TrainingSample]) -> None:
        if self._prepared_split_hash is not None:
            raise RuntimeError("training samples are sealed")
        self._counts_by_target.clear()
        values = tuple(samples)
        column_width = self._column_features + int(self._sequence_layout is not None)
        placeholders = ", ".join("?" for _ in range(5 + column_width + len(SAMPLE_TARGET_COLUMNS)))
        self._connection.run_many(
            f"INSERT INTO samples VALUES ({placeholders})",  # noqa: S608
            (self._encode(sample) for sample in values),
        )
        for (day, industry), group in groupby(
            sorted(values, key=lambda item: (item.trade_date, item.industry)),
            key=lambda item: (item.trade_date, item.industry),
        ):
            population = tuple(group)
            statistics = [(EncodeDate(day), industry, "rows", len(population), 0.0, 0.0)]
            for position, target in enumerate(SAMPLE_TARGET_COLUMNS):
                labels = tuple(item.targets[position] for item in population if item.targets[position] is not None)
                statistics.append(
                    (
                        EncodeDate(day),
                        industry,
                        target,
                        len(labels),
                        math.fsum(cast(float, value) for value in labels),
                        math.fsum(cast(float, value) ** 2 for value in labels),
                    )
                )
            self._connection.run_many(
                "INSERT INTO sample_statistics VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(trade_date,industry,target) DO UPDATE SET "
                "sample_count=sample_count+excluded.sample_count,target_sum=target_sum+excluded.target_sum,"
                "target_sum_squares=target_sum_squares+excluded.target_sum_squares",
                statistics,
            )

    def record_day(self, day: date, candidates: int, missing: tuple[int, ...]) -> None:
        if self._prepared_split_hash is not None:
            raise RuntimeError("training samples are sealed")
        if len(missing) != 5:
            raise ValueError("sample diagnostics require five horizons")
        self._connection.run(
            "INSERT INTO sample_day_diagnostics VALUES (?, ?, ?, ?, ?, ?, ?)", (EncodeDate(day), candidates, *missing)
        )

    def prepare_for_model_fitting(self, split: TrainingDateSplit) -> None:
        if self._prepared_split_hash is not None:
            raise RuntimeError("training sample split is already prepared")
        self._counts_by_target.clear()
        split_dates = (
            *((EncodeDate(day), "training") for day in split.model_fit_dates),
            *((EncodeDate(day), "early_stopping") for day in split.early_stopping_dates),
            *((EncodeDate(day), "calibration") for day in split.calibration_dates),
            *((EncodeDate(day), "validation") for day in split.confirmation_dates),
            *((EncodeDate(day), "holdout") for day in split.daily_proxy_holdout_dates),
        )
        self._connection.run_many("INSERT INTO sample_split_dates VALUES (?, ?)", split_dates)
        if self._sequence_writer is not None:
            self._sequence_writer.finish()
        self._connection.run("CREATE INDEX samples_industry_date ON samples(industry, trade_date, code)")
        self._connection.run("UPDATE sample_contract SET split_hash=? WHERE singleton=1", (EncodeHash(split.content_hash),))
        self._transaction.commit()
        self._prepared_split_hash = split.content_hash

    def require_split(self, split: TrainingDateSplit) -> None:
        self.require_split_hash(split.content_hash)

    def require_split_hash(self, split_hash: str) -> None:
        if self._prepared_split_hash != split_hash:
            raise RuntimeError("training sample split identity is inconsistent")

    def industry_counts(self, contract: TrainedHeadContract) -> tuple[TrainingIndustryCounts, ...]:
        target = _require_target_column(contract.target_column)
        if not self._counts_by_target:
            self._prepare_industry_counts()
        return self._counts_by_target[target]

    def iter_batches(
        self,
        industry: str,
        contract: TrainedHeadContract,
        split_name: TrainingSplitName,
        *,
        batch_size: int = 256,
    ) -> Iterator[TrainingSampleBatch]:
        """Stream one industry's ordered partition without allocating its full matrix."""
        yield from self._iter_batches(contract, split_name, industry=industry, batch_size=batch_size)

    def iter_validation_batches(
        self, contract: TrainedHeadContract, *, batch_size: int = 256
    ) -> Iterator[TrainingSampleBatch]:
        """Read only the diagnostic confirmation proxy, in complete same-day population order."""
        yield from self._iter_batches(contract, "validation", industry=None, batch_size=batch_size)

    def _iter_batches(
        self,
        contract: TrainedHeadContract,
        split_name: TrainingSplitName,
        *,
        industry: str | None,
        batch_size: int,
    ) -> Iterator[TrainingSampleBatch]:
        if not 1 <= batch_size <= 256:
            raise ValueError("training batch size must be within 1..256")
        if split_name == "holdout":
            raise ValueError("training_holdout_is_sealed")
        target = _require_target_column(contract.target_column)
        self._feature_columns(contract)
        shared = self._shared_rows
        if shared is not None and industry == shared.industry and split_name != "validation":
            target_position = SAMPLE_TARGET_COLUMNS.index(target)
            selected_rows = [
                position for position, partition in enumerate(shared.splits)
                if partition == split_name and not np.isnan(shared.targets[position, target_position])
            ]
            for start in range(0, len(selected_rows), batch_size):
                row_positions = selected_rows[start:start + batch_size]
                yield TrainingSampleBatch(
                    shared.features[np.ix_(row_positions, contract.feature_positions)],
                    shared.targets[row_positions, target_position],
                    tuple(shared.dates[position] for position in row_positions),
                    tuple(shared.codes[position] for position in row_positions),
                    (shared.industry,) * len(row_positions),
                )
            return
        if self._sequence_layout is not None:
            columns = ",".join(f's."{name}"' for name in self._feature_names[:self._column_features]) + ",s.sequence_start"
        else:
            columns = ",".join(f's."{name}"' for name in self._feature_columns(contract))
        selected = "AND s.industry=? " if industry is not None else ""
        rows = self._connection.run(
            f"SELECT s.trade_date,s.code,s.industry,{columns},s.{target} FROM samples s "
            "JOIN sample_split_dates d ON d.trade_date=s.trade_date "
            f"WHERE d.split_name=? {selected}AND s.{target} IS NOT NULL ORDER BY s.trade_date,s.code",
            (split_name, industry) if industry is not None else (split_name,),
        )
        decoder = SampleSequenceReader(self._connection, self._sequence_layout) if self._sequence_layout else None
        positions = contract.feature_positions
        feature_selection: slice | tuple[int, ...] = positions
        if (
            decoder is not None
            and positions
            and 0 <= positions[0] <= positions[-1] < self._feature_count
            and positions == tuple(range(positions[0], positions[-1] + 1))
        ):
            feature_selection = slice(positions[0], positions[-1] + 1)
        try:
            while batch := rows.fetch_values(batch_size):
                codes = tuple(str(row[1]) for row in batch)
                if decoder is None:
                    matrix = np.asarray([row[3:] for row in batch], dtype=np.float64)
                    features, labels = matrix[:, :-1], matrix[:, -1]
                else:
                    features = np.empty((len(batch), self._feature_count), dtype=np.float64)
                    features[:, :self._column_features] = [row[3 : 3 + self._column_features] for row in batch]
                    decoder.read(
                        cast(tuple[tuple[str, int], ...], tuple((str(row[1]), row[-2]) for row in batch)),
                        output=features[:, self._column_features:],
                    )
                    features = features[:, feature_selection]
                    labels = np.asarray([row[-1] for row in batch], dtype=np.float64)
                yield TrainingSampleBatch(
                    features,
                    labels,
                    self._decode_dates(cast(tuple[int, ...], tuple(row[0] for row in batch))),
                    codes,
                    tuple(str(row[2]) for row in batch),
                )
        finally:
            rows.close()

    def _decode_dates(self, values: tuple[int, ...]) -> tuple[date, ...]:
        missing = set(values).difference(self._date_values)
        self._date_values.update((value, DecodeDate(value)) for value in missing)
        return tuple(self._date_values[value] for value in values)

    def _prepare_industry_counts(self) -> None:
        aggregates = ",".join(
            f"SUM(CASE WHEN target='{target}' THEN sample_count ELSE 0 END),"
            f"COUNT(DISTINCT CASE WHEN target='{target}' AND sample_count>0 THEN samples.trade_date END)"
            for target in SAMPLE_TARGET_COLUMNS
        )
        rows = self._connection.run(
            f"SELECT samples.industry, splits.split_name, {aggregates} FROM sample_statistics samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            "GROUP BY samples.industry, splits.split_name ORDER BY samples.industry, splits.split_name"
        )
        grouped: dict[str, dict[str, dict[TrainingSplitName, tuple[int, int]]]] = {
            target: {} for target in SAMPLE_TARGET_COLUMNS
        }
        for industry, split_name, *counts in rows:
            for index, target in enumerate(SAMPLE_TARGET_COLUMNS):
                count, dates = counts[index * 2 : index * 2 + 2]
                if count:
                    grouped[target].setdefault(str(industry), {})[cast(TrainingSplitName, split_name)] = (
                        int(count),
                        int(dates),
                    )
        for target, industries in grouped.items():
            self._counts_by_target[target] = tuple(
                TrainingIndustryCounts(
                    industry,
                    values.get("training", (0, 0))[0],
                    values.get("early_stopping", (0, 0))[0],
                    values.get("calibration", (0, 0))[0],
                    values.get("validation", (0, 0))[0],
                    values.get("calibration", (0, 0))[1],
                    values.get("holdout", (0, 0))[0],
                )
                for industry, values in industries.items()
            )

    @contextmanager
    def share_industry(self, industry: str, *, checkpoint: Callable[[], None] | None = None) -> Iterator[None]:
        """Decode common rows once, project independent heads, release at industry end."""
        if self._shared_rows is not None:
            raise RuntimeError("training industry read already in progress")
        predicate = "FROM samples s JOIN sample_split_dates d ON d.trade_date=s.trade_date WHERE s.industry=? AND d.split_name IN ('training','early_stopping','calibration')"
        count = int(self._connection.run(f"SELECT COUNT(*) {predicate}", (industry,)).require_one()[0])
        with ExitStack() as resources:
            width = self._feature_count + len(SAMPLE_TARGET_COLUMNS)
            directory = Path(resources.enter_context(TemporaryDirectory(prefix="sample-shared-", dir=self.workspace)))
            matrix = _empty_matrix(count, width, "common", directory, resources).features
            columns = ",".join(f's."{name}"' for name in self._feature_names[:self._column_features])
            if self._sequence_layout is not None:
                columns += ",s.sequence_start"
            targets = ",".join(f"s.{name}" for name in SAMPLE_TARGET_COLUMNS)
            cursor = self._connection.run(
                f"SELECT d.split_name,s.trade_date,s.code,{columns},{targets} {predicate} ORDER BY s.trade_date,s.code",
                (industry,),
            )
            decoder = SampleSequenceReader(self._connection, self._sequence_layout) if self._sequence_layout else None
            dates: list[date] = []
            codes: list[str] = []
            splits: list[str] = []
            position = 0
            try:
                while rows := cursor.fetch_values(256):
                    if checkpoint is not None:
                        checkpoint()
                    end = position + len(rows)
                    if end > count:
                        raise RuntimeError("training sample count changed during shared read")
                    matrix[position:end, :self._column_features] = [row[3:3 + self._column_features] for row in rows]
                    if decoder is not None:
                        decoder.read(
                            cast(tuple[tuple[str, int], ...], tuple((str(row[2]), row[3 + self._column_features]) for row in rows)),
                            output=matrix[position:end, self._column_features:self._feature_count],
                        )
                    matrix[position:end, self._feature_count:] = np.asarray([row[-len(SAMPLE_TARGET_COLUMNS):] for row in rows], dtype=np.float64)
                    dates.extend(self._decode_dates(cast(tuple[int, ...], tuple(row[1] for row in rows))))
                    codes.extend(str(row[2]) for row in rows)
                    splits.extend(str(row[0]) for row in rows)
                    position = end
            finally:
                cursor.close()
            if position != count:
                raise RuntimeError("training sample count changed during shared read")
            matrix.flags.writeable = False
            self._shared_rows = _IndustryRows(industry, matrix[:, :self._feature_count], matrix[:, self._feature_count:], tuple(dates), tuple(codes), tuple(splits))
            try:
                yield
            finally:
                self._shared_rows = None

    @contextmanager
    def industry_data(
        self,
        counts: TrainingIndustryCounts,
        contract: TrainedHeadContract,
    ) -> Iterator[TrainingIndustryData]:
        width = len(contract.feature_positions)
        self._feature_columns(contract)
        size = (counts.training + counts.early_stopping + counts.calibration) * (width + 1) * 8
        with ExitStack() as resources:
            directory = (
                Path(resources.enter_context(TemporaryDirectory(prefix="sample-fit-", dir=self.workspace)))
                if size > TRAINING_MATRIX_BYTES else None
            )
            matrices = {
                name: _empty_matrix(count, width, name, directory, resources)
                for name, count in (("training", counts.training), ("early_stopping", counts.early_stopping),
                                    ("calibration", counts.calibration))
            }
            yield self._read_industry_data(counts, contract, matrices)

    def _read_industry_data(
        self, counts: TrainingIndustryCounts, contract: TrainedHeadContract,
        matrices: dict[str, TrainingSampleMatrix],
    ) -> TrainingIndustryData:
        positions: dict[TrainingSplitName, int] = {
            "training": 0,
            "early_stopping": 0,
            "calibration": 0,
        }
        calibration_dates: list[date] = []
        if self._sequence_layout is not None or self._shared_rows is not None:
            for partition, matrix in matrices.items():
                position = 0
                for batch in self.iter_batches(counts.industry, contract, cast(TrainingSplitName, partition)):
                    end = position + len(batch.labels)
                    matrix.features[position:end] = batch.features
                    matrix.labels[position:end] = batch.labels
                    position = end
                    if partition == "calibration":
                        calibration_dates.extend(batch.trade_dates)
                if position != len(matrix.labels):
                    raise RuntimeError("training sample split count changed during its read")
            return TrainingIndustryData(
                matrices["training"],
                matrices["early_stopping"],
                matrices["calibration"],
                counts.validation,
                tuple(calibration_dates),
            )
        feature_sql = ", ".join(f'samples."{name}"' for name in self._feature_columns(contract))
        target = _require_target_column(contract.target_column)
        cursor = self._connection.run(
            f"SELECT splits.split_name, samples.trade_date, {feature_sql}, samples.{target} FROM samples "  # noqa: S608
            "JOIN sample_split_dates AS splits ON splits.trade_date=samples.trade_date "
            f"WHERE samples.industry=? AND samples.{target} IS NOT NULL "  # noqa: S608
            "AND splits.split_name IN ('training', 'early_stopping', 'calibration') "
            "ORDER BY samples.trade_date, samples.code",
            (counts.industry,),
        )
        try:
            while rows := cursor.fetch_values(4_096):
                for name, group in groupby(rows, key=lambda row: row[0]):
                    split_name = cast(TrainingSplitName, name)
                    split_rows = tuple(group)
                    position = positions[split_name]
                    end = position + len(split_rows)
                    matrix = matrices[split_name]
                    if end > len(matrix.labels):
                        raise RuntimeError("training sample split count changed during its read")
                    values = np.asarray([row[2:] for row in split_rows], dtype=np.float64)
                    matrix.features[position:end] = values[:, :-1]
                    matrix.labels[position:end] = values[:, -1]
                    positions[split_name] = end
                    if split_name == "calibration":
                        calibration_dates.extend(self._decode_dates(cast(tuple[int, ...], tuple(row[1] for row in split_rows))))
        finally:
            cursor.close()
        expected = {
            "training": counts.training,
            "early_stopping": counts.early_stopping,
            "calibration": counts.calibration,
        }
        if positions != expected:
            raise RuntimeError("training sample split count changed during its read")
        return TrainingIndustryData(
            matrices["training"],
            matrices["early_stopping"],
            matrices["calibration"],
            counts.validation,
            tuple(calibration_dates),
        )

    def split_count(self, split_name: TrainingSplitName, contract: TrainedHeadContract) -> int:
        target = _require_target_column(contract.target_column)
        row = self._connection.run(
            "SELECT COALESCE(SUM(samples.sample_count),0) FROM sample_statistics samples JOIN sample_split_dates AS splits "
            "ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.target=?",
            (split_name, target),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def split_distinct_date_count(self, split_name: TrainingSplitName, contract: TrainedHeadContract) -> int:
        target = _require_target_column(contract.target_column)
        row = self._connection.run(
            "SELECT COUNT(DISTINCT samples.trade_date) FROM sample_statistics samples "
            "JOIN sample_split_dates AS splits "
            "ON splits.trade_date=samples.trade_date WHERE splits.split_name=? AND samples.target=? AND samples.sample_count>0",
            (split_name, target),
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def validation_target_metrics(self) -> tuple[TargetMetric, ...]:
        metrics: list[TargetMetric] = []
        rows = self._connection.run(
            "SELECT target,COALESCE(SUM(sample_count),0),SUM(target_sum),SUM(target_sum_squares) FROM sample_statistics s "
            "JOIN sample_split_dates d ON d.trade_date=s.trade_date WHERE d.split_name='validation' GROUP BY target"
        ).fetchall()
        for target in SAMPLE_TARGET_COLUMNS:
            row = next((tuple(value)[1:] for value in rows if value[0] == target), (0, 0.0, 0.0))
            count = int(row[0])
            mean = float(row[1]) / count if count else None
            second = float(row[2]) / count if count else None
            deviation = math.sqrt(max(0.0, second - mean * mean)) if mean is not None and second is not None else None
            metrics.append(TargetMetric(target, count, mean, deviation))
        return tuple(metrics)

    def count(self) -> int:
        row = self._connection.run("SELECT COALESCE(SUM(sample_count),0) FROM sample_statistics WHERE target='rows'").fetchone()
        return int(row[0]) if row is not None else 0

    def coverage(self, contract: TrainedHeadContract) -> tuple[SampleCoverage, ...]:
        result: list[SampleCoverage] = []
        for partition in ("training", "early_stopping", "calibration", "validation", "holdout"):
            rows = self._connection.run(
                "SELECT d.trade_date,COALESCE(SUM(CASE WHEN s.target='rows' THEN s.sample_count END),0),"
                "COALESCE(SUM(CASE WHEN s.target=? THEN s.sample_count END),0),"
                "COALESCE(g.candidate_count,0),g.missing_t1,g.missing_t2,g.missing_t3,g.missing_t4,g.missing_t5 "
                "FROM sample_split_dates d LEFT JOIN sample_statistics s ON s.trade_date=d.trade_date "
                "LEFT JOIN sample_day_diagnostics g ON g.trade_date=d.trade_date "
                "WHERE d.split_name=? GROUP BY d.trade_date ORDER BY d.trade_date",
                (_require_target_column(contract.target_column), partition),
            ).fetchall()
            gaps = tuple(
                SampleDateGap(
                    DecodeDate(row[0]),
                    int(row[1]),
                    int(row[3]),
                    tuple(int(value or 0) for value in row[4:]),
                )
                for row in rows
                if not row[2]
            )
            result.append(
                SampleCoverage(
                    contract.target_column,
                    partition,
                    len(rows),
                    len(rows) - len(gaps),
                    sum(int(row[1]) for row in rows),
                    sum(int(row[2]) for row in rows),
                    gaps,
                )
            )
        return tuple(result)

    @property
    def database_size_bytes(self) -> int:
        return self._path.stat().st_size

    @property
    def workspace(self) -> Path:
        return self._path.parent

    def _encode(self, sample: TrainingSample) -> tuple[object, ...]:
        if len(sample.features) != self._feature_count:
            raise ValueError("training sample feature width is invalid")
        encoded_features: tuple[float | int, ...] = sample.features
        if self._sequence_writer is not None:
            start = self._sequence_writer.append(sample.code, sample.features[self._column_features :])
            encoded_features = (*sample.features[: self._column_features], start)
        return (
            EncodeDate(sample.trade_date),
            sample.code,
            sample.board,
            sample.industry,
            sample.average_amount_20d,
            *encoded_features,
            *sample.targets,
        )


def _require_target_column(value: str) -> str:
    if value not in SAMPLE_TARGET_COLUMNS:
        raise ValueError("training target column is invalid")
    return value


def _empty_matrix(
    row_count: int, width: int, name: str, directory: Path | None, resources: ExitStack,
) -> TrainingSampleMatrix:
    if directory is None or row_count == 0:
        return TrainingSampleMatrix(np.empty((row_count, width), dtype=np.float64), np.empty(row_count, dtype=np.float64))
    arrays = []
    for suffix, shape in (("features", (row_count, width)), ("labels", (row_count,))):
        size = math.prod(shape) * 8
        with (directory / f"{name}-{suffix}.bin").open("w+b") as handle:
            handle.truncate(size)
            buffer = mmap.mmap(handle.fileno(), size)
        resources.callback(buffer.close)
        arrays.append(np.ndarray(shape, dtype=np.float64, buffer=buffer))
    return TrainingSampleMatrix(arrays[0], arrays[1])


__all__ = [
    "SQLiteTrainingSampleIndex",
    "TRAINING_SAMPLE_CACHE_MIB",
    "TRAINING_SAMPLE_MMAP_BYTES",
    "TargetMetric",
    "TrainingIndustryCounts",
    "TrainingIndustryData",
    "TrainingSample",
    "TrainingSampleMatrix",
    "TrainingSampleBatch",
    "TrainingSplitName",
]
