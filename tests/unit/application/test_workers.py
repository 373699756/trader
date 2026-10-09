from __future__ import annotations

import threading

import pytest

from trader.infra.shutdown import ShutdownDeadline
from trader.infra.workers import (
    BoundedExecutor,
    BoundedExecutorStatus,
    WorkerResourceRejectedError,
    submit_or_reject,
    injected_executor,
)


def test_injected_executor_rejects_stopped_pool_and_runs_nested_call_without_new_workers() -> None:
    pool = BoundedExecutor(worker_count=1, queue_capacity=1, thread_name_prefix="injected-test")
    with pytest.raises(WorkerResourceRejectedError, match="resource_rejected"):
        submit_or_reject(injected_executor(pool), threading.get_ident).result()
    assert injected_executor(None).submit(threading.get_ident).result() == threading.get_ident()
    pool.start()
    try:

        def nested() -> tuple[int, int]:
            future = injected_executor(pool).submit(threading.get_ident)
            assert future is not None
            return threading.get_ident(), future.result(timeout=1.0)

        future = pool.submit(nested)
        assert future is not None
        caller, nested_thread = future.result(timeout=1.0)
        assert caller == nested_thread
        assert pool.status().submitted_count == 1
    finally:
        pool.stop()
    with pytest.raises(WorkerResourceRejectedError, match="resource_rejected"):
        submit_or_reject(injected_executor(pool), threading.get_ident).result()


def test_bounded_executor_rejects_over_capacity_and_stops_all_workers() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        queue_capacity=1,
        thread_name_prefix="test-bounded",
    )
    release = threading.Event()
    entered = threading.Barrier(3)

    def blocking_task() -> str:
        entered.wait(timeout=1.0)
        release.wait(timeout=1.0)
        return threading.current_thread().name

    assert executor.start() is True
    assert executor.start() is False
    assert len([thread for thread in threading.enumerate() if thread.name.startswith("test-bounded")]) == 2
    first = executor.submit(blocking_task)
    second = executor.submit(blocking_task)
    assert first is not None
    assert second is not None
    entered.wait(timeout=1.0)
    queued = executor.submit(lambda: threading.current_thread().name)
    assert queued is not None
    assert executor.submit(lambda: None) is None
    assert executor.status().rejected_count == 1

    release.set()
    assert first.result(timeout=1.0).startswith("test-bounded")
    assert second.result(timeout=1.0).startswith("test-bounded")
    assert queued.result(timeout=1.0).startswith("test-bounded")
    executor.stop()

    assert executor.submit(lambda: None) is None
    assert executor.status() == BoundedExecutorStatus(
        workers=2,
        urgent_workers=0,
        queue_capacity=1,
        urgent_queue_capacity=0,
        inflight=0,
        urgent_inflight=0,
        submitted_count=3,
        urgent_submitted_count=0,
        completed_count=3,
        urgent_completed_count=0,
        rejected_count=2,
        urgent_rejected_count=0,
        running=False,
    )
    assert not any(thread.name.startswith("test-bounded") for thread in threading.enumerate())


def test_urgent_lane_runs_while_normal_lane_is_saturated() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        urgent_worker_count=1,
        queue_capacity=2,
        thread_name_prefix="test-urgent",
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_task() -> None:
        entered.set()
        release.wait(timeout=1.0)

    executor.start()
    try:
        normal = executor.submit(blocking_task)
        assert normal is not None
        assert entered.wait(timeout=1.0)
        urgent = executor.submit_urgent(lambda: threading.current_thread().name)
        assert urgent is not None
        assert urgent.result(timeout=0.2).startswith("test-urgent-urgent")
        assert executor.status().urgent_completed_count == 1
    finally:
        release.set()
        executor.stop()

    assert not any(thread.name.startswith("test-urgent") for thread in threading.enumerate())


def test_urgent_lane_has_one_bounded_waiting_slot() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        urgent_worker_count=1,
        queue_capacity=2,
        thread_name_prefix="test-urgent-bound",
    )
    entered = threading.Event()
    release = threading.Event()

    def blocking_task() -> None:
        entered.set()
        release.wait(timeout=1.0)

    executor.start()
    try:
        running = executor.submit_urgent(blocking_task)
        assert running is not None
        assert entered.wait(timeout=1.0)
        queued = executor.submit_urgent(lambda: 42)
        assert queued is not None
        assert executor.submit_urgent(lambda: None) is None
        assert executor.status().urgent_rejected_count == 1
        release.set()
        assert queued.result(timeout=1.0) == 42
    finally:
        release.set()
        executor.stop()


def test_partial_worker_start_failure_releases_started_threads(monkeypatch) -> None:
    executor = BoundedExecutor(
        worker_count=2,
        queue_capacity=1,
        thread_name_prefix="test-start-failure",
    )
    original_start = threading.Thread.start
    starts = 0

    def fail_second_worker(thread: threading.Thread) -> None:
        nonlocal starts
        if thread.name.startswith("test-start-failure"):
            starts += 1
            if starts == 2:
                raise RuntimeError("simulated thread start failure")
        original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second_worker)

    with pytest.raises(RuntimeError, match="simulated thread start failure"):
        executor.start()

    assert not any(thread.name.startswith("test-start-failure") for thread in threading.enumerate())


def test_nested_injected_pool_does_not_wait_on_its_own_worker() -> None:
    executor = BoundedExecutor(
        worker_count=1,
        queue_capacity=2,
        thread_name_prefix="test-nested-shared",
    )
    assert executor.start() is True

    def nested_fetch() -> int:
        borrowed = injected_executor(executor)
        future = borrowed.submit(lambda: 42)
        assert future is not None
        return future.result(timeout=1.0)

    try:
        future = executor.submit(nested_fetch)
        assert future is not None
        assert future.result(timeout=1.0) == 42
    finally:
        executor.stop()

    assert not any(thread.name.startswith("test-nested-") for thread in threading.enumerate())


def test_nested_injected_pool_does_not_wait_even_with_spare_worker() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        queue_capacity=2,
        thread_name_prefix="test-nested-spare",
    )
    assert executor.start() is True

    def nested_fetch() -> tuple[str, str]:
        outer_thread = threading.current_thread().name
        borrowed = injected_executor(executor)
        future = borrowed.submit(lambda: threading.current_thread().name)
        assert future is not None
        return outer_thread, future.result(timeout=1.0)

    try:
        future = executor.submit(nested_fetch)
        assert future is not None
        outer_thread, inner_thread = future.result(timeout=1.0)
        assert outer_thread.startswith("test-nested-spare")
        assert inner_thread.startswith("test-nested-spare")
        assert inner_thread == outer_thread
    finally:
        executor.stop()

    assert not any(thread.name.startswith("test-nested-") for thread in threading.enumerate())


def test_all_shared_pool_workers_can_run_nested_work_without_waiting_on_their_own_queue() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        queue_capacity=2,
        thread_name_prefix="test-nested-multi",
    )
    entered = threading.Barrier(2)
    assert executor.start() is True

    def nested_fetch() -> int:
        entered.wait(timeout=1.0)
        borrowed = injected_executor(executor)
        future = borrowed.submit(lambda: 42)
        assert future is not None
        return future.result(timeout=0.2)

    try:
        futures = tuple(executor.submit(nested_fetch) for _index in range(2))
        assert all(future is not None for future in futures)
        assert tuple(future.result(timeout=1.0) for future in futures if future is not None) == (42, 42)
    finally:
        executor.stop()

    assert not any(thread.name.startswith("test-nested-") for thread in threading.enumerate())


def test_executor_deadline_cancels_pending_and_never_waits_for_blocked_running_task() -> None:
    executor = BoundedExecutor(
        worker_count=1,
        queue_capacity=2,
        thread_name_prefix="test-deadline",
    )
    entered = threading.Event()
    release = threading.Event()

    def blocked() -> None:
        entered.set()
        release.wait()

    executor.start()
    running = executor.submit(blocked)
    pending = executor.submit(lambda: 42)
    assert running is not None
    assert pending is not None
    assert entered.wait(timeout=1.0)

    try:
        step = executor.stop(
            deadline=ShutdownDeadline.start(0.05),
            cancel_futures=True,
        )

        assert step.name == "test-deadline"
        assert step.completed is False
        assert step.timed_out is True
        assert step.cancelled_count == 1
        assert pending.cancelled() is True
    finally:
        release.set()
        running.result(timeout=1.0)


def test_full_injected_queue_rejects_without_running_on_caller() -> None:
    executor = BoundedExecutor(worker_count=1, queue_capacity=0, thread_name_prefix="test-rejected")
    release = threading.Event()
    entered = threading.Event()
    calls: list[int] = []

    def blocking_task() -> None:
        entered.set()
        release.wait(timeout=1.0)

    executor.start()
    try:
        running = executor.submit(blocking_task)
        assert running is not None
        assert entered.wait(timeout=1.0)
        rejected = submit_or_reject(injected_executor(executor), calls.append, threading.get_ident())
        with pytest.raises(WorkerResourceRejectedError, match="resource_rejected"):
            rejected.result(timeout=0.1)
        assert calls == []
        assert executor.status().rejected_count == 1
    finally:
        release.set()
        executor.stop()
