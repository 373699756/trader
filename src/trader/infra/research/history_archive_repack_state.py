"""Typed state for the BaoStock monthly-archive physical repack."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

HistoryArchiveActivationState = Literal[
    "prepared",
    "old_partitions_moved",
    "old_control_moved",
    "new_partitions_activated",
    "new_control_activated",
    "verified",
    "finalized",
    "rolled_back",
]
HistoryArchiveRepackAction = Literal["build", "activate", "rollback", "finalize"]
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class HistoryArchiveSourceFile:
    relative_path: str
    size_bytes: int
    modified_ns: int

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        if path.is_absolute() or ".." in path.parts or self.size_bytes < 0 or self.modified_ns < 0:
            raise ValueError("history repack source file identity is invalid")


@dataclass(frozen=True)
class HistoryArchiveRepackPartition:
    relative_path: str
    source_sha256: str
    target_sha256: str
    row_count: int
    observation_count: int
    latest_row_count: int
    source_bytes: int
    target_bytes: int
    records_logical_hash: str
    observations_logical_hash: str

    def __post_init__(self) -> None:
        path = PurePosixPath(self.relative_path)
        hashes = (
            self.source_sha256,
            self.target_sha256,
            self.records_logical_hash,
            self.observations_logical_hash,
        )
        if (
            path.is_absolute()
            or len(path.parts) != 3
            or path.parts[0] != "partitions"
            or path.suffix != ".sqlite3"
            or any(_SHA256.fullmatch(value) is None for value in hashes)
            or min(
                self.row_count,
                self.observation_count,
                self.latest_row_count,
                self.source_bytes,
                self.target_bytes,
            )
            < 0
        ):
            raise ValueError("history repack partition evidence is invalid")


@dataclass(frozen=True)
class HistoryArchiveRepackBuildState:
    source_root: str
    target_root: str
    source_snapshot_hash: str
    source_sequence: int
    source_file_identity_hash: str
    source_files: tuple[HistoryArchiveSourceFile, ...]
    source_bytes: int
    security_count: int
    trading_day_count: int
    expected_partition_count: int
    page_size: int
    partitions: tuple[HistoryArchiveRepackPartition, ...]
    target_snapshot_hash: str | None = None
    completed: bool = False

    def __post_init__(self) -> None:
        paths = tuple(item.relative_path for item in self.partitions)
        source_paths = tuple(item.relative_path for item in self.source_files)
        if (
            not self.source_root
            or not self.target_root
            or _SHA256.fullmatch(self.source_snapshot_hash) is None
            or _SHA256.fullmatch(self.source_file_identity_hash) is None
            or self.source_sequence < 1
            or source_paths != tuple(sorted(set(source_paths)))
            or len(source_paths) != self.expected_partition_count + 1
            or self.source_bytes < 1
            or self.security_count < 1
            or self.trading_day_count < 1
            or self.expected_partition_count < 1
            or self.page_size != 8192
            or paths != tuple(sorted(set(paths)))
            or len(paths) > self.expected_partition_count
            or self.completed != (self.target_snapshot_hash is not None)
            or self.target_snapshot_hash is not None
            and _SHA256.fullmatch(self.target_snapshot_hash) is None
        ):
            raise ValueError("history repack build state is invalid")

    @property
    def target_bytes(self) -> int:
        return sum(item.target_bytes for item in self.partitions)


@dataclass(frozen=True)
class HistoryArchiveActivationJournal:
    state: HistoryArchiveActivationState
    source_root: str
    target_root: str
    backup_root: str
    source_snapshot_hash: str
    target_snapshot_hash: str

    def __post_init__(self) -> None:
        if (
            not self.source_root
            or not self.target_root
            or not self.backup_root
            or _SHA256.fullmatch(self.source_snapshot_hash) is None
            or _SHA256.fullmatch(self.target_snapshot_hash) is None
        ):
            raise ValueError("history repack activation journal is invalid")

    @property
    def fenced(self) -> bool:
        return self.state not in {"finalized", "rolled_back"}


@dataclass(frozen=True)
class HistoryArchiveRepackRequirements:
    expected_partition_count: int = 100
    expected_trading_days: int = 2000
    target_page_size: int = 8192
    maximum_target_bytes: int = 16 * 1024 * 1024 * 1024
    minimum_reduction_ratio: float = 0.30
    reserve_bytes: int = 2 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        if (
            self.expected_partition_count < 1
            or self.expected_trading_days < 1
            or self.target_page_size != 8192
            or self.maximum_target_bytes < 1
            or not -1 <= self.minimum_reduction_ratio < 1
            or self.reserve_bytes < 0
        ):
            raise ValueError("history repack requirements are invalid")


@dataclass(frozen=True)
class HistoryArchiveRepackStatus:
    action: HistoryArchiveRepackAction
    state: str
    source_snapshot_hash: str
    target_snapshot_hash: str | None
    completed_partitions: int
    total_partitions: int
    source_bytes: int
    target_bytes: int
    released_bytes: int = 0

    def __post_init__(self) -> None:
        if (
            not self.state
            or _SHA256.fullmatch(self.source_snapshot_hash) is None
            or self.target_snapshot_hash is not None
            and _SHA256.fullmatch(self.target_snapshot_hash) is None
            or not 0 <= self.completed_partitions <= self.total_partitions
            or self.total_partitions < 1
            or min(self.source_bytes, self.target_bytes, self.released_bytes) < 0
        ):
            raise ValueError("history repack status is invalid")


@dataclass(frozen=True)
class HistoryTrainingMemoryEvidence:
    training_status: str
    repeat_training_status: str
    training_input_hash: str
    model_hash: str
    report_hash: str
    peak_rss_bytes: int
    max_rss_bytes: int

    def __post_init__(self) -> None:
        if (
            self.training_status != "engineering_ready"
            or self.repeat_training_status != "already_current"
            or any(
                _SHA256.fullmatch(value) is None
                for value in (self.training_input_hash, self.model_hash, self.report_hash)
            )
            or self.peak_rss_bytes < 1
            or self.max_rss_bytes < 1
            or self.peak_rss_bytes > self.max_rss_bytes
            or self.max_rss_bytes > 2048 * 1024 * 1024
        ):
            raise ValueError("Tomorrow training memory evidence is invalid")


__all__ = [
    "HistoryArchiveActivationJournal",
    "HistoryArchiveActivationState",
    "HistoryArchiveRepackAction",
    "HistoryArchiveRepackBuildState",
    "HistoryArchiveRepackPartition",
    "HistoryArchiveRepackRequirements",
    "HistoryArchiveRepackStatus",
    "HistoryArchiveSourceFile",
    "HistoryTrainingMemoryEvidence",
]
