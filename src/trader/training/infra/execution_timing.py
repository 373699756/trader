"""Job-local timings around the existing stream and resource reservation ports."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field, replace
from threading import Lock, get_native_id
import time
from typing import Literal, TypeVar

from trader.recommendation.domain.scoring.profile_identity import SCORING_PROFILE_IDS, ScoringProfileId
from trader.training.application.resources import (
    TrainingCpuAdmission,
    TrainingExecutionTiming,
    TrainingMemoryAdmission,
    TrainingProfileTiming,
    TrainingResourceTiming,
    TrainingSampleBuildTiming,
)

ValueT = TypeVar("ValueT")


@dataclass
class _ResourceTiming:
    wait_seconds: float = 0.0
    hold_seconds: float = 0.0
    acquired_count: int = 0
    failed_acquisitions: int = 0
    inflight_count: int = 0

    def snapshot(self) -> TrainingResourceTiming:
        return TrainingResourceTiming(
            self.wait_seconds,
            self.hold_seconds,
            self.acquired_count,
            self.failed_acquisitions,
            self.inflight_count,
        )


@dataclass
class _ProfileTiming:
    profile: ScoringProfileId
    started_after_seconds: float | None = None
    finished_after_seconds: float | None = None
    queue_send_seconds: float = 0.0
    queue_receive_seconds: float = 0.0
    cpu: _ResourceTiming = field(default_factory=_ResourceTiming)
    memory: _ResourceTiming = field(default_factory=_ResourceTiming)
    cpu_held_while_memory_wait_seconds: float = 0.0
    native_id: int | None = None
    thread_cpu_seconds: float | None = None
    sample_build: TrainingSampleBuildTiming = field(default_factory=TrainingSampleBuildTiming)
    first_batch_after_seconds: float | None = None
    queue_full_wait_seconds: float = 0.0
    queue_empty_wait_seconds: float = 0.0
    queue_full_wait_count: int = 0
    queue_empty_wait_count: int = 0
    queue_peak_batches: int = 0

    def snapshot(self) -> TrainingProfileTiming:
        return TrainingProfileTiming(
            self.profile,
            self.started_after_seconds,
            self.finished_after_seconds,
            self.queue_send_seconds,
            self.queue_receive_seconds,
            self.cpu.snapshot(),
            self.memory.snapshot(),
            self.cpu_held_while_memory_wait_seconds,
            native_id=self.native_id,
            thread_cpu_seconds=self.thread_cpu_seconds,
            sample_build=self.sample_build,
            first_batch_after_seconds=self.first_batch_after_seconds,
            queue_full_wait_seconds=self.queue_full_wait_seconds,
            queue_empty_wait_seconds=self.queue_empty_wait_seconds,
            queue_full_wait_count=self.queue_full_wait_count,
            queue_empty_wait_count=self.queue_empty_wait_count,
            queue_peak_batches=self.queue_peak_batches,
        )


class TrainingExecutionMeasurements:
    """Observe port scopes without changing admission, cancellation or publication."""

    def __init__(
        self,
        profiles: tuple[ScoringProfileId, ...],
        monotonic: Callable[[], float],
        *,
        thread_time: Callable[[], float] = time.thread_time,
        native_id: Callable[[], int] = get_native_id,
    ) -> None:
        if not profiles or len(set(profiles)) != len(profiles) or not set(profiles) <= set(SCORING_PROFILE_IDS):
            raise ValueError("training_measurement_profiles_invalid")
        self._profiles = tuple(_ProfileTiming(profile) for profile in profiles)
        self._monotonic = monotonic
        self._thread_time = thread_time
        self._native_id = native_id
        self._started = monotonic()
        self._lock = Lock()
        self._source_read_seconds = 0.0
        self._source_read_thread_cpu_seconds = 0.0
        self._peak_cpu_reservations = 0
        self._peak_memory_reservations = 0

    def _profile(self, profile: ScoringProfileId) -> _ProfileTiming:
        return next(item for item in self._profiles if item.profile == profile)

    def snapshot(self) -> TrainingExecutionTiming:
        with self._lock:
            return TrainingExecutionTiming(
                self._source_read_seconds,
                self._peak_cpu_reservations,
                self._peak_memory_reservations,
                tuple(item.snapshot() for item in self._profiles),
                source_read_thread_cpu_seconds=self._source_read_thread_cpu_seconds,
            )

    @contextmanager
    def task(self, profile: ScoringProfileId) -> Iterator[None]:
        state = self._profile(profile)
        started_cpu = self._thread_time()
        native_id = self._native_id()
        with self._lock:
            state.started_after_seconds = self._monotonic() - self._started
            state.native_id = native_id
            state.thread_cpu_seconds = None
        try:
            yield
        finally:
            elapsed_cpu = self._thread_time() - started_cpu
            with self._lock:
                state.finished_after_seconds = self._monotonic() - self._started
                state.thread_cpu_seconds = elapsed_cpu

    @contextmanager
    def read_source(self) -> Iterator[None]:
        started = self._monotonic()
        started_cpu = self._thread_time()
        try:
            yield
        finally:
            elapsed = self._monotonic() - started
            elapsed_cpu = self._thread_time() - started_cpu
            with self._lock:
                self._source_read_seconds += elapsed
                self._source_read_thread_cpu_seconds += elapsed_cpu

    @contextmanager
    def send_queue(self, profile: ScoringProfileId) -> Iterator[None]:
        state = self._profile(profile)
        started = self._monotonic()
        try:
            yield
        finally:
            elapsed = self._monotonic() - started
            with self._lock:
                state.queue_send_seconds += elapsed

    @contextmanager
    def receive_queue(self, profile: ScoringProfileId) -> Iterator[None]:
        state = self._profile(profile)
        started = self._monotonic()
        try:
            yield
        finally:
            elapsed = self._monotonic() - started
            with self._lock:
                state.queue_receive_seconds += elapsed

    @contextmanager
    def sample_build(self, profile: ScoringProfileId) -> Iterator[None]:
        state = self._profile(profile)
        started = self._monotonic()
        started_cpu = self._thread_time()
        with self._lock:
            sample = state.sample_build
            state.sample_build = replace(
                sample,
                count=sample.count + 1,
                inflight_count=sample.inflight_count + 1,
                first_started_after_seconds=sample.first_started_after_seconds
                if sample.first_started_after_seconds is not None else started - self._started,
            )
        completed = False
        try:
            yield
            completed = True
        finally:
            finished = self._monotonic()
            elapsed_cpu = self._thread_time() - started_cpu
            with self._lock:
                sample = state.sample_build
                state.sample_build = replace(
                    sample,
                    completed_count=sample.completed_count + int(completed),
                    inflight_count=sample.inflight_count - 1,
                    wall_seconds=(sample.wall_seconds or 0.0) + finished - started,
                    thread_cpu_seconds=(sample.thread_cpu_seconds or 0.0) + elapsed_cpu,
                    last_finished_after_seconds=finished - self._started,
                )

    @contextmanager
    def wait_queue(self, profile: ScoringProfileId, reason: Literal["full", "empty"]) -> Iterator[None]:
        state = self._profile(profile)
        started = self._monotonic()
        try:
            yield
        finally:
            elapsed = self._monotonic() - started
            with self._lock:
                if reason == "full":
                    state.queue_full_wait_seconds += elapsed
                    state.queue_full_wait_count += 1
                else:
                    state.queue_empty_wait_seconds += elapsed
                    state.queue_empty_wait_count += 1

    def received_batch(self, profile: ScoringProfileId) -> None:
        with self._lock:
            state = self._profile(profile)
            if state.first_batch_after_seconds is None:
                state.first_batch_after_seconds = self._monotonic() - self._started

    def queue_depth(self, profile: ScoringProfileId, batches: int) -> None:
        # Called under the queue mutex. No measurement-lock holder accesses a queue.
        with self._lock:
            state = self._profile(profile)
            state.queue_peak_batches = max(state.queue_peak_batches, batches)

    def cpu_admission(self, profile: ScoringProfileId, admission: TrainingCpuAdmission) -> TrainingCpuAdmission:
        return _MeasuredCpuAdmission(self, profile, admission)

    def memory_admission(
        self, profile: ScoringProfileId, admission: TrainingMemoryAdmission
    ) -> TrainingMemoryAdmission:
        return _MeasuredMemoryAdmission(self, profile, admission)

    @contextmanager
    def reservation(
        self,
        profile: ScoringProfileId,
        resource: Literal["cpu", "memory"],
        reservation: AbstractContextManager[ValueT],
    ) -> Iterator[ValueT]:
        state = self._profile(profile)
        timing = state.cpu if resource == "cpu" else state.memory
        acquired: float | None = None
        started = self._monotonic()
        with self._lock:
            cpu_held = resource == "memory" and state.cpu.inflight_count > 0
        try:
            with reservation as granted:
                acquired = self._monotonic()
                with self._lock:
                    timing.wait_seconds += acquired - started
                    timing.acquired_count += 1
                    timing.inflight_count += 1
                    if cpu_held:
                        state.cpu_held_while_memory_wait_seconds += acquired - started
                    if resource == "cpu":
                        self._peak_cpu_reservations = max(
                            self._peak_cpu_reservations, sum(item.cpu.inflight_count for item in self._profiles)
                        )
                    else:
                        self._peak_memory_reservations = max(
                            self._peak_memory_reservations, sum(item.memory.inflight_count for item in self._profiles)
                        )
                try:
                    yield granted
                finally:
                    finished = self._monotonic()
                    with self._lock:
                        timing.hold_seconds += finished - acquired
                        timing.inflight_count -= 1
        finally:
            finished = self._monotonic()
            with self._lock:
                if acquired is None:
                    timing.wait_seconds += finished - started
                    timing.failed_acquisitions += 1
                    if cpu_held:
                        state.cpu_held_while_memory_wait_seconds += finished - started


@dataclass(frozen=True)
class _MeasuredCpuAdmission:
    measurements: TrainingExecutionMeasurements
    profile: ScoringProfileId
    admission: TrainingCpuAdmission

    def reserve(self, requested_threads: int, heartbeat: Callable[[], None]) -> AbstractContextManager[int]:
        return self.measurements.reservation(self.profile, "cpu", self.admission.reserve(requested_threads, heartbeat))


@dataclass(frozen=True)
class _MeasuredMemoryAdmission:
    measurements: TrainingExecutionMeasurements
    profile: ScoringProfileId
    admission: TrainingMemoryAdmission

    def reserve(self, required_bytes: int, heartbeat: Callable[[], None]) -> AbstractContextManager[None]:
        return self.measurements.reservation(self.profile, "memory", self.admission.reserve(required_bytes, heartbeat))


__all__ = ["TrainingExecutionMeasurements"]
