"""One reservation scope for fitting, with explicit candidate acquisition and release order."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass

from trader.training.application.resources import TrainingCpuAdmission, TrainingMemoryAdmission


@dataclass(frozen=True)
class TrainingFittingReservation:
    compute_threads: int
    _cpu: ExitStack

    def release_cpu(self) -> None:
        """Return fitting CPU before file publication while retaining owned memory."""
        self._cpu.close()


@contextmanager
def ReserveTrainingFittingResources(
    cpu_admission: TrainingCpuAdmission | None,
    memory_admission: TrainingMemoryAdmission | None,
    requested_threads: int,
    required_bytes: Callable[[], int],
    heartbeat: Callable[[], None],
    *,
    memory_first: bool,
) -> Iterator[TrainingFittingReservation]:
    if type(requested_threads) is not int or requested_threads < 1:
        raise ValueError("training_cpu_request_invalid")
    with ExitStack() as resources:
        cpu = ExitStack()
        if memory_first and memory_admission is not None:
            resources.enter_context(memory_admission.reserve(required_bytes(), heartbeat))
        resources.enter_context(cpu)
        threads = requested_threads
        if cpu_admission is not None:
            threads = cpu.enter_context(cpu_admission.reserve(requested_threads, heartbeat))
        if not memory_first and memory_admission is not None:
            resources.enter_context(memory_admission.reserve(required_bytes(), heartbeat))
        yield TrainingFittingReservation(threads, cpu)


__all__ = [
    "ReserveTrainingFittingResources",
    "TrainingFittingReservation",
]
