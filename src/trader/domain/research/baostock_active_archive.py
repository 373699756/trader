"""Typed identities for a sealed BaoStock parent plus immutable increments."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal

from trader.domain.research.h1_point_in_time import canonical_hash

BaoStockArchiveFieldFamily = Literal[
    "daily_raw",
    "daily_qfq",
    "is_st",
    "industry",
    "qualification",
    "hard_filter",
    "risk_facts",
]
BaoStockIncrementCheckpointState = Literal["completed", "failed"]

BAOSTOCK_ARCHIVE_FIELD_FAMILIES: tuple[BaoStockArchiveFieldFamily, ...] = (
    "daily_raw",
    "daily_qfq",
    "is_st",
    "industry",
    "qualification",
    "hard_filter",
    "risk_facts",
)

_CODE = re.compile(r"^[0-9]{6}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE = re.compile(r"^[a-z0-9_]{1,64}$")
_PARTITION_ID = re.compile(r"^[a-z0-9_-]{1,64}$")


def _require_hash(value: str, label: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"BaoStock {label} must be SHA-256")


@dataclass(frozen=True, order=True)
class BaoStockArchiveRecordKey:
    code: str
    trade_date: date
    family: BaoStockArchiveFieldFamily

    def __post_init__(self) -> None:
        if _CODE.fullmatch(self.code) is None or self.family not in BAOSTOCK_ARCHIVE_FIELD_FAMILIES:
            raise ValueError("BaoStock archive record key is invalid")


@dataclass(frozen=True)
class BaoStockIncrementCheckpoint:
    key: BaoStockArchiveRecordKey
    state: BaoStockIncrementCheckpointState
    content_hash: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        completed = self.state == "completed" and self.content_hash is not None and self.error_code is None
        failed = self.state == "failed" and self.content_hash is None and self.error_code is not None
        if not (completed or failed):
            raise ValueError("BaoStock increment checkpoint is invalid")
        if self.content_hash is not None:
            _require_hash(self.content_hash, "checkpoint content hash")
        if self.error_code is not None and _ERROR_CODE.fullmatch(self.error_code) is None:
            raise ValueError("BaoStock increment checkpoint error code is invalid")


@dataclass(frozen=True)
class BaoStockFieldCoverage:
    family: BaoStockArchiveFieldFamily
    reusable_rows: int
    incremental_rows: int
    missing_rows: int
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        counts = (self.reusable_rows, self.incremental_rows, self.missing_rows)
        if (
            self.family not in BAOSTOCK_ARCHIVE_FIELD_FAMILIES
            or any(isinstance(value, bool) or value < 0 for value in counts)
            or (self.missing_rows > 0) != (self.missing_reason is not None)
            or (self.missing_reason is not None and _ERROR_CODE.fullmatch(self.missing_reason) is None)
        ):
            raise ValueError("BaoStock field coverage is invalid")


@dataclass(frozen=True)
class BaoStockActiveArchiveContext:
    parent_manifest_hash: str
    parent_manifest_file_hash: str
    source_cutoff: date
    calendar_hash: str
    source_identity_hash: str
    partition_by_code: tuple[tuple[str, str], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.parent_manifest_hash, "parent manifest hash"),
            (self.parent_manifest_file_hash, "parent manifest file hash"),
            (self.calendar_hash, "calendar hash"),
            (self.source_identity_hash, "source identity hash"),
        ):
            _require_hash(value, label)
        partitions = tuple(sorted(self.partition_by_code))
        if (
            not partitions
            or len({code for code, _partition in partitions}) != len(partitions)
            or any(
                _CODE.fullmatch(code) is None or _PARTITION_ID.fullmatch(partition) is None
                for code, partition in partitions
            )
        ):
            raise ValueError("BaoStock active archive partition map is invalid")
        object.__setattr__(self, "partition_by_code", partitions)
        object.__setattr__(self, "content_hash", canonical_hash(self))

    def partition_for(self, code: str) -> str:
        partition = next((value for current, value in self.partition_by_code if current == code), None)
        if partition is None:
            raise ValueError("BaoStock code is outside the active archive universe")
        return partition


@dataclass(frozen=True)
class BaoStockIncrementPartition:
    relative_path: str
    sha256: str
    record_count: int
    checkpoint_count: int
    schema_version: str = "baostock_increment_partition"

    def __post_init__(self) -> None:
        path = Path(self.relative_path)
        _require_hash(self.sha256, "increment partition hash")
        if (
            path.is_absolute()
            or ".." in path.parts
            or len(path.parts) != 2
            or path.parts[0] != "shards"
            or path.suffix != ".sqlite3"
            or min(self.record_count, self.checkpoint_count) < 0
            or self.record_count > self.checkpoint_count
            or self.schema_version != "baostock_increment_partition"
        ):
            raise ValueError("BaoStock increment partition is invalid")


@dataclass(frozen=True)
class BaoStockIncrementManifest:
    parent_manifest_hash: str
    parent_manifest_file_hash: str
    source_cutoff: date
    calendar_hash: str
    source_identity_hash: str
    partitions: tuple[BaoStockIncrementPartition, ...]
    records_hash: str
    checkpoints_hash: str
    production_authority: bool = False
    point_in_time_parity: bool = False
    schema_version: str = "baostock_increment_manifest"
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.parent_manifest_hash, "parent manifest hash"),
            (self.parent_manifest_file_hash, "parent manifest file hash"),
            (self.calendar_hash, "calendar hash"),
            (self.source_identity_hash, "source identity hash"),
            (self.records_hash, "increment records hash"),
            (self.checkpoints_hash, "increment checkpoints hash"),
        ):
            _require_hash(value, label)
        partitions = tuple(sorted(self.partitions, key=lambda item: item.relative_path))
        if (
            len({item.relative_path for item in partitions}) != len(partitions)
            or self.production_authority
            or self.point_in_time_parity
            or self.schema_version != "baostock_increment_manifest"
        ):
            raise ValueError("BaoStock increment manifest is invalid")
        object.__setattr__(self, "partitions", partitions)
        object.__setattr__(self, "content_hash", canonical_hash(self))


@dataclass(frozen=True)
class BaoStockActiveManifest:
    parent_manifest_hash: str
    parent_manifest_file_hash: str
    increment_manifest_hash: str
    increment_manifest_path: str
    source_cutoff: date
    calendar_hash: str
    source_identity_hash: str
    field_coverage: tuple[BaoStockFieldCoverage, ...]
    production_authority: bool = False
    point_in_time_parity: bool = False
    schema_version: str = "baostock_active_manifest"
    active_data_hash: str = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for value, label in (
            (self.parent_manifest_hash, "parent manifest hash"),
            (self.parent_manifest_file_hash, "parent manifest file hash"),
            (self.increment_manifest_hash, "increment manifest hash"),
            (self.calendar_hash, "calendar hash"),
            (self.source_identity_hash, "source identity hash"),
        ):
            _require_hash(value, label)
        path = Path(self.increment_manifest_path)
        expected = Path("increments") / self.increment_manifest_hash / "manifest.json"
        coverage = tuple(
            sorted(self.field_coverage, key=lambda item: BAOSTOCK_ARCHIVE_FIELD_FAMILIES.index(item.family))
        )
        if (
            path != expected
            or len(coverage) != len(BAOSTOCK_ARCHIVE_FIELD_FAMILIES)
            or tuple(item.family for item in coverage) != BAOSTOCK_ARCHIVE_FIELD_FAMILIES
            or self.production_authority
            or self.point_in_time_parity
            or self.schema_version != "baostock_active_manifest"
        ):
            raise ValueError("BaoStock active manifest increment identity is invalid")
        object.__setattr__(self, "field_coverage", coverage)
        active_hash = canonical_hash(
            (
                self.parent_manifest_hash,
                self.parent_manifest_file_hash,
                self.increment_manifest_hash,
                self.source_cutoff,
                self.calendar_hash,
                self.source_identity_hash,
                coverage,
            )
        )
        object.__setattr__(self, "active_data_hash", active_hash)
        object.__setattr__(self, "content_hash", canonical_hash(self))


__all__ = [
    "BAOSTOCK_ARCHIVE_FIELD_FAMILIES",
    "BaoStockActiveArchiveContext",
    "BaoStockActiveManifest",
    "BaoStockArchiveFieldFamily",
    "BaoStockArchiveRecordKey",
    "BaoStockFieldCoverage",
    "BaoStockIncrementCheckpoint",
    "BaoStockIncrementCheckpointState",
    "BaoStockIncrementManifest",
    "BaoStockIncrementPartition",
]
