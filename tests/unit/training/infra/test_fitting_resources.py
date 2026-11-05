"""CPU and memory lifetimes remain observable across fitting, failure and publication."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Event

import pytest

from tests.helpers.training_resources import TrainingResourceSettingsFixture
from trader.training.infra.cpu_admission import TrainingCpuBudget
from trader.training.infra.fitting_resources import ReserveTrainingFittingResources
from trader.training.infra.memory_admission import TrainingMemoryBudget


@dataclass
class _HeldResources:
    cpu: bool = False
    memory: bool = False
    events: list[str] = field(default_factory=list)


@dataclass
class _ObservedCpu:
    held: _HeldResources

    @contextmanager
    def reserve(self, requested_threads: int, heartbeat: Callable[[], None]) -> Iterator[int]:
        heartbeat()
        self.held.cpu = True
        self.held.events.append("cpu_acquired")
        try:
            yield min(requested_threads, 1)
        finally:
            self.held.cpu = False
            self.held.events.append("cpu_released")


@dataclass
class _ObservedMemory:
    held: _HeldResources

    @contextmanager
    def reserve(self, required_bytes: int, heartbeat: Callable[[], None]) -> Iterator[None]:
        assert required_bytes == 100
        heartbeat()
        self.held.memory = True
        self.held.events.append("memory_acquired")
        try:
            yield
        finally:
            self.held.memory = False
            self.held.events.append("memory_released")


def _bounded_heartbeat() -> Callable[[], None]:
    deadline = time.monotonic() + 2

    def check() -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture_resource_not_released")

    return check


@pytest.mark.parametrize("memory_first", (False, True))
def test_reservation_orders_estimation_and_admission_without_changing_granted_threads(memory_first: bool) -> None:
    held = _HeldResources()

    def estimate() -> int:
        assert held.cpu is not memory_first
        held.events.append("estimated")
        return 100

    with ReserveTrainingFittingResources(
        _ObservedCpu(held), _ObservedMemory(held), 2, estimate, lambda: None, memory_first=memory_first,
    ) as reservation:
        assert (held.cpu, held.memory, reservation.compute_threads) == (True, True, 1)
    assert (held.cpu, held.memory) == (False, False)
    assert held.events == (
        ["estimated", "memory_acquired", "cpu_acquired", "cpu_released", "memory_released"]
        if memory_first
        else ["cpu_acquired", "estimated", "memory_acquired", "memory_released", "cpu_released"]
    )


@pytest.mark.parametrize("memory_first", (False, True))
@pytest.mark.parametrize("failure_stage", ("estimate", "memory_admission"))
def test_failed_memory_preparation_leaves_no_cpu_and_candidate_never_takes_it(
    memory_first: bool, failure_stage: str,
) -> None:
    held = _HeldResources()

    def estimate() -> int:
        assert held.cpu is not memory_first
        if failure_stage == "estimate":
            raise RuntimeError("fixture_estimate_failed")
        return 101

    failure = RuntimeError if failure_stage == "estimate" else ValueError
    with pytest.raises(failure), ReserveTrainingFittingResources(
        _ObservedCpu(held), TrainingMemoryBudget(100), 2, estimate, lambda: None, memory_first=memory_first,
    ):
        pytest.fail("failed memory request entered fitting")
    assert (held.cpu, held.memory) == (False, False)
    assert held.events == ([] if memory_first else ["cpu_acquired", "cpu_released"])


def test_cancelled_candidate_memory_wait_never_holds_cpu_and_all_capacity_is_reusable() -> None:
    cpu = TrainingCpuBudget(4, TrainingResourceSettingsFixture().cpu)
    memory = TrainingMemoryBudget(100)
    waiting, cancelled = Event(), Event()

    def heartbeat() -> None:
        waiting.set()
        if cancelled.is_set():
            raise InterruptedError("fixture_cancelled")

    def fit() -> None:
        with ReserveTrainingFittingResources(
            cpu, memory, 2, lambda: 100, heartbeat, memory_first=True,
        ):
            pytest.fail("fitting entered while memory was unavailable")

    with ThreadPoolExecutor(max_workers=1) as executor:
        with memory.reserve(100, lambda: None):
            future = executor.submit(fit)
            try:
                assert waiting.wait(2)
                assert cpu.peak_reserved_threads == 0
            finally:
                cancelled.set()
            with pytest.raises(InterruptedError, match="fixture_cancelled"):
                future.result(timeout=2)
            assert cpu.peak_reserved_threads == 0
    with cpu.reserve(cpu.capacity, _bounded_heartbeat()), memory.reserve(100, _bounded_heartbeat()):
        pass


@pytest.mark.parametrize("memory_first", (False, True))
@pytest.mark.parametrize("failure", (RuntimeError, InterruptedError))
def test_fitting_failure_and_cancellation_return_both_complete_budgets(
    memory_first: bool, failure: type[Exception],
) -> None:
    cpu = TrainingCpuBudget(4, TrainingResourceSettingsFixture().cpu)
    memory = TrainingMemoryBudget(100)
    with pytest.raises(failure, match="fixture_fit_failed"), ReserveTrainingFittingResources(
        cpu, memory, 2, lambda: 100, _bounded_heartbeat(), memory_first=memory_first,
    ) as reservation:
        assert reservation.compute_threads == 2
        raise failure("fixture_fit_failed")
    with cpu.reserve(cpu.capacity, _bounded_heartbeat()), memory.reserve(100, _bounded_heartbeat()):
        pass


@pytest.mark.parametrize("memory_first", (False, True))
def test_early_cpu_release_retains_memory_and_is_idempotent(memory_first: bool) -> None:
    held = _HeldResources()
    with ReserveTrainingFittingResources(
        _ObservedCpu(held), _ObservedMemory(held), 2, lambda: 100, lambda: None, memory_first=memory_first,
    ) as reservation:
        assert held.cpu and held.memory
        reservation.release_cpu()
        assert not held.cpu and held.memory
        reservation.release_cpu()
        assert not held.cpu and held.memory
        assert held.events.count("cpu_released") == 1
        assert "memory_released" not in held.events
    reservation.release_cpu()
    assert not held.cpu and not held.memory
    assert held.events.count("cpu_released") == held.events.count("memory_released") == 1


def test_absent_admission_ports_do_not_estimate_memory() -> None:
    def unexpected_estimation() -> int:
        pytest.fail("memory estimate is unnecessary without a memory admission port")

    with ReserveTrainingFittingResources(
        None, None, 2, unexpected_estimation, lambda: None, memory_first=True,
    ) as reservation:
        assert reservation.compute_threads == 2
        reservation.release_cpu()
        reservation.release_cpu()
