"""Explicit reservation boundary for concurrent fitting allocations."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId


@dataclass(frozen=True)
class TrainingProfileThreads:
    v1: int
    v2: int
    v3: int

    def __post_init__(self) -> None:
        if any(type(value) is not int or value < 1 for value in (self.v1, self.v2, self.v3)):
            raise ValueError("training_profile_threads_invalid")

    def for_profile(self, profile: ScoringProfileId) -> int:
        if profile == "v1":
            return self.v1
        if profile == "v2":
            return self.v2
        if profile == "v3":
            return self.v3
        raise ValueError("training_profile_threads_require_v1_v2_v3")


@dataclass(frozen=True)
class TrainingCpuSettings:
    coordinator_cpus: int
    maximum_threads: int | None
    native_threads: int
    profile_threads: TrainingProfileThreads

    def __post_init__(self) -> None:
        if (
            type(self.coordinator_cpus) is not int
            or self.coordinator_cpus < 0
            or type(self.native_threads) is not int
            or self.native_threads < 1
            or not isinstance(self.profile_threads, TrainingProfileThreads)
            or (
                self.maximum_threads is not None
                and (type(self.maximum_threads) is not int or self.maximum_threads < 1)
            )
        ):
            raise ValueError("training_cpu_settings_invalid")


@dataclass(frozen=True)
class TrainingMemorySettings:
    maximum_bytes: int
    reserve_bytes: int
    minimum_available_bytes: int

    def __post_init__(self) -> None:
        if (
            type(self.maximum_bytes) is not int
            or self.maximum_bytes < 1
            or type(self.reserve_bytes) is not int
            or self.reserve_bytes < 0
            or type(self.minimum_available_bytes) is not int
            or not 1 <= self.minimum_available_bytes <= self.maximum_bytes
        ):
            raise ValueError("training_memory_settings_invalid")


@dataclass(frozen=True)
class TrainingFanoutSettings:
    batch_rows: int
    queue_batches: int

    def __post_init__(self) -> None:
        if (
            type(self.batch_rows) is not int
            or self.batch_rows < 1
            or type(self.queue_batches) is not int
            or self.queue_batches < 1
        ):
            raise ValueError("training_fanout_settings_invalid")


@dataclass(frozen=True)
class TrainingSampleSettings:
    sqlite_cache_bytes: int
    sqlite_mmap_bytes: int
    matrix_memory_bytes: int
    sequence_cache_bytes: int
    sequence_writer_codes: int
    v1_sqlite_cache_bytes: int
    v1_batch_rows: int
    tcn_batch_memory_bytes: int

    def __post_init__(self) -> None:
        nonnegative = (
            self.sqlite_mmap_bytes, self.matrix_memory_bytes, self.sequence_cache_bytes, self.tcn_batch_memory_bytes,
        )
        caches = (self.sqlite_cache_bytes, self.v1_sqlite_cache_bytes)
        if (
            any(type(value) is not int or value < 0 for value in nonnegative)
            or any(type(value) is not int or value < 1024 or value % 1024 for value in caches)
            or type(self.sequence_writer_codes) is not int or self.sequence_writer_codes < 1
            or type(self.v1_batch_rows) is not int or self.v1_batch_rows < 1
        ):
            raise ValueError("training_sample_settings_invalid")


@dataclass(frozen=True)
class TrainingResourceSettings:
    cpu: TrainingCpuSettings
    memory: TrainingMemorySettings
    fanout: TrainingFanoutSettings
    samples: TrainingSampleSettings


@dataclass(frozen=True)
class TrainingResourceTiming:
    wait_seconds: float
    hold_seconds: float
    acquired_count: int
    failed_acquisitions: int
    inflight_count: int


@dataclass(frozen=True)
class TrainingSampleBuildTiming:
    count: int = 0
    completed_count: int = 0
    inflight_count: int = 0
    wall_seconds: float | None = None
    thread_cpu_seconds: float | None = None
    first_started_after_seconds: float | None = None
    last_finished_after_seconds: float | None = None


@dataclass(frozen=True)
class TrainingProfileTiming:
    profile: ScoringProfileId
    started_after_seconds: float | None
    finished_after_seconds: float | None
    queue_send_seconds: float
    queue_receive_seconds: float
    cpu: TrainingResourceTiming
    memory: TrainingResourceTiming
    cpu_held_while_memory_wait_seconds: float
    native_id: int | None = None
    # Only this task's calling thread; unavailable until its scope has exited.
    thread_cpu_seconds: float | None = None
    sample_build: TrainingSampleBuildTiming = field(default_factory=TrainingSampleBuildTiming, kw_only=True)
    first_batch_after_seconds: float | None = field(default=None, kw_only=True)
    queue_full_wait_seconds: float = field(default=0.0, kw_only=True)
    queue_empty_wait_seconds: float = field(default=0.0, kw_only=True)
    queue_full_wait_count: int = field(default=0, kw_only=True)
    queue_empty_wait_count: int = field(default=0, kw_only=True)
    queue_peak_batches: int = field(default=0, kw_only=True)


@dataclass(frozen=True)
class TrainingExecutionTiming:
    source_read_seconds: float
    peak_cpu_reservations: int
    peak_memory_reservations: int
    profiles: tuple[TrainingProfileTiming, ...]
    # None denotes a manually constructed timing without this observation.
    source_read_thread_cpu_seconds: float | None = None


class TrainingExecutionObserver(Protocol):
    def task(self, profile: ScoringProfileId) -> AbstractContextManager[None]: ...

    def read_source(self) -> AbstractContextManager[None]: ...

    def send_queue(self, profile: ScoringProfileId) -> AbstractContextManager[None]: ...

    def receive_queue(self, profile: ScoringProfileId) -> AbstractContextManager[None]: ...

    def sample_build(self, profile: ScoringProfileId) -> AbstractContextManager[None]: ...

    def wait_queue(self, profile: ScoringProfileId, reason: Literal["full", "empty"]) -> AbstractContextManager[None]: ...

    def received_batch(self, profile: ScoringProfileId) -> None: ...

    def queue_depth(self, profile: ScoringProfileId, batches: int) -> None: ...


class TrainingMemoryAdmission(Protocol):
    def reserve(self, required_bytes: int, heartbeat: Callable[[], None]) -> AbstractContextManager[None]: ...


class TrainingCpuAdmission(Protocol):
    def reserve(self, requested_threads: int, heartbeat: Callable[[], None]) -> AbstractContextManager[int]: ...
