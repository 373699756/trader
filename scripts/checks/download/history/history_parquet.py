"""Offline Parquet candidate, derived exclusively from a pinned SQLite publication.

This experiment has no production caller. SQLite remains the only fact owner.
"""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import hashlib
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import cast

import polars as pl

from trader.download.domain.baostock_daily import BaoStockBoard, BaoStockCellStatus
from trader.download.domain.history_revision import HistoryTrainingPoint, HistoryTrainingRow
from trader.download.infra.history_snapshot_inspection import LoadPublishedHistorySnapshot, PublishedHistorySnapshot
from trader.download.infra.history_snapshot_reader import HistorySnapshotReader
from trader.infra.sqlite_codec import DecodeDate, EncodeDate

_VERSION = 1
_SCHEMA = {
    "trade_date": pl.Int32, "code": pl.String, "board": pl.String, "industry": pl.String,
    "cell_status": pl.String, "point_present": pl.Boolean,
    "qfq_close": pl.Float64, "qfq_amount": pl.Float64, "qfq_open": pl.Float64,
    "qfq_high": pl.Float64, "qfq_low": pl.Float64, "raw_volume": pl.Float64,
}


@dataclass(frozen=True)
class ParquetMonth:
    month: str
    source_hash: str
    file_hash: str
    row_count: int

    @property
    def filename(self) -> str:
        return f"{self.month}-{self.source_hash}.parquet"


@dataclass(frozen=True)
class ParquetPublication:
    snapshot_hash: str
    months: tuple[ParquetMonth, ...]


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _sync_directory(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record(row: HistoryTrainingRow) -> tuple[object, ...]:
    point = row.point
    return (
        EncodeDate(row.trade_date), row.code, row.board, row.industry, row.cell_status, point is not None,
        *((point.qfq_close_price, point.qfq_amount, point.qfq_open, point.qfq_high, point.qfq_low, point.raw_volume)
          if point is not None else (None,) * 6),
    )


class ParquetHistoryProjection:
    def __init__(self, root: Path, *, sqlite_policy: SqliteIoPolicy) -> None:
        self._sqlite_policy = sqlite_policy
        self.root = root

    def load(self) -> ParquetPublication:
        value = json.loads((self.root / "manifest.json").read_text())
        if set(value) != {"version", "snapshot_hash", "months"} or value["version"] != _VERSION:
            raise ValueError("parquet_manifest_format_invalid")
        months = tuple(ParquetMonth(**item) for item in value["months"])
        keys = tuple(item.month for item in months)
        hashes = (value["snapshot_hash"], *(item.source_hash for item in months), *(item.file_hash for item in months))
        if (
            keys != tuple(sorted(set(keys))) or not keys
            or any(len(key) != 7 or key[4] != "-" or not (key[:4] + key[5:]).isdigit()
                   or not 1 <= int(key[5:]) <= 12 for key in keys)
            or any(not isinstance(item, str) or len(item) != 64
                   or any(char not in "0123456789abcdef" for char in item) for item in hashes)
            or any(type(item.row_count) is not int or item.row_count < 0 for item in months)
        ):
            raise ValueError("parquet_manifest_identity_invalid")
        return ParquetPublication(value["snapshot_hash"], months)

    def publish(self, history: PublishedHistorySnapshot) -> ParquetPublication:
        if LoadPublishedHistorySnapshot(history.root, sequence=history.snapshot.sequence, sqlite_policy=self._sqlite_policy) != history:
            raise ValueError("parquet_source_is_not_a_published_snapshot")
        self.root.mkdir(parents=True, exist_ok=True)
        previous = self.load() if (self.root / "manifest.json").exists() else None
        old = {item.source_hash: item for item in previous.months} if previous else {}
        reader = HistorySnapshotReader(history.root, sqlite_policy=self._sqlite_policy)
        codes = tuple(item.code for item in history.universe.securities)
        months = []
        for part in history.snapshot.partitions:
            month = f"{part.calendar_year:04d}-{part.calendar_month:02d}"
            dates = tuple(day for day in history.calendar.open_dates if day.strftime("%Y-%m") == month)
            if not dates:
                continue
            # Reference changes invalidate industry/eligibility projections too.
            source_hash = hashlib.sha256(json.dumps((
                _VERSION, month, part.logical_hash, part.row_count,
                history.snapshot.source_identity_hash, history.snapshot.universe_hash,
                tuple(EncodeDate(day) for day in dates),
            ), separators=(",", ":")).encode()).hexdigest()
            known = old.get(source_hash)
            if known is not None:
                self._validate_month(known)
                months.append(known)
                continue
            records = []
            for row in reader.iter_training_rows(history.snapshot, dates, codes):
                records.append(_record(row))
                if len(records) > 310_000:
                    raise ValueError("parquet_probe_month_limit_exceeded")
            frame = pl.DataFrame(records, schema=_SCHEMA, orient="row").sort("trade_date", "code")
            with NamedTemporaryFile(dir=self.root, prefix=".month-", suffix=".parquet", delete=False) as target:
                temporary = Path(target.name)
            try:
                frame.write_parquet(temporary, compression="zstd", compression_level=1, row_group_size=65_536)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                loaded = pl.read_parquet(temporary)
                if loaded.schema != frame.schema or not loaded.equals(frame):
                    raise ValueError("parquet_write_verification_failed")
                item = ParquetMonth(month, source_hash, _file_hash(temporary), frame.height)
                final = self.root / item.filename
                if final.exists():
                    self._validate_month(item)
                else:
                    os.replace(temporary, final)
                months.append(item)
            finally:
                temporary.unlink(missing_ok=True)
        publication = ParquetPublication(history.snapshot.content_hash, tuple(months))
        self._publish_manifest(publication)
        return publication

    def _publish_manifest(self, publication: ParquetPublication) -> None:
        payload = {
            "version": _VERSION, "snapshot_hash": publication.snapshot_hash,
            "months": [
                {"month": item.month, "source_hash": item.source_hash, "file_hash": item.file_hash,
                 "row_count": item.row_count} for item in publication.months
            ],
        }
        with NamedTemporaryFile(dir=self.root, prefix=".manifest-", mode="w", delete=False) as handle:
            temporary = Path(handle.name)
            try:
                json.dump(payload, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                _sync_directory(self.root)
                os.replace(temporary, self.root / "manifest.json")
                _sync_directory(self.root)
            finally:
                temporary.unlink(missing_ok=True)

    def _validate_month(self, item: ParquetMonth) -> None:
        path = self.root / item.filename
        if _file_hash(path) != item.file_hash:
            raise ValueError("parquet_month_hash_mismatch")
        scan = pl.scan_parquet(path)
        if scan.collect_schema() != pl.Schema(_SCHEMA) or scan.select(pl.len()).collect().item() != item.row_count:
            raise ValueError("parquet_month_schema_or_count_mismatch")

    def iter_rows(self, history: PublishedHistorySnapshot, dates: tuple[date, ...]) -> Iterator[HistoryTrainingRow]:
        publication = self.load()
        if publication.snapshot_hash != history.snapshot.content_hash or not dates:
            raise ValueError("parquet_snapshot_mismatch")
        selected = frozenset(dates)
        previous: tuple[date, str] | None = None
        for item in publication.months:
            if not dates[0].strftime("%Y-%m") <= item.month <= dates[-1].strftime("%Y-%m"):
                continue
            self._validate_month(item)
            scan = pl.scan_parquet(self.root / item.filename).filter(
                pl.col("trade_date").is_between(EncodeDate(dates[0]), EncodeDate(dates[-1]))
            ).select(*_SCHEMA)
            for batch in scan.collect_batches(chunk_size=4096, maintain_order=True, engine="streaming"):
                for record in batch.iter_rows():
                    day = DecodeDate(record[0])
                    key = (day, record[1])
                    if day not in selected or previous is not None and key <= previous:
                        raise ValueError("parquet_row_order_or_calendar_invalid")
                    previous = key
                    point = HistoryTrainingPoint(day, *record[6:]) if record[5] else None
                    yield HistoryTrainingRow(
                        record[1], day, cast(BaoStockBoard, record[2]), record[3], point,
                        cast(BaoStockCellStatus | None, record[4]),
                    )
