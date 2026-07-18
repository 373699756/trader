from __future__ import annotations

import threading

import pytest

from trader.application.workers import BoundedExecutor, borrow_executor


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
    assert executor.status()["rejected_count"] == 1

    release.set()
    assert first.result(timeout=1.0).startswith("test-bounded")
    assert second.result(timeout=1.0).startswith("test-bounded")
    assert queued.result(timeout=1.0).startswith("test-bounded")
    executor.stop()

    assert executor.submit(lambda: None) is None
    assert executor.status() == {
        "workers": 2,
        "queue_capacity": 1,
        "inflight": 0,
        "submitted_count": 3,
        "completed_count": 3,
        "rejected_count": 2,
        "running": False,
    }
    assert not any(thread.name.startswith("test-bounded") for thread in threading.enumerate())


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


def test_nested_shared_pool_borrow_does_not_wait_on_its_own_worker() -> None:
    executor = BoundedExecutor(
        worker_count=1,
        queue_capacity=2,
        thread_name_prefix="test-nested-shared",
    )
    assert executor.start() is True

    def nested_fetch() -> int:
        with borrow_executor(
            executor,
            worker_count=1,
            thread_name_prefix="test-nested-local",
            queue_capacity=2,
        ) as borrowed:
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


def test_nested_borrow_uses_spare_shared_worker() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        queue_capacity=2,
        thread_name_prefix="test-nested-spare",
    )
    assert executor.start() is True

    def nested_fetch() -> tuple[str, str]:
        outer_thread = threading.current_thread().name
        with borrow_executor(
            executor,
            worker_count=1,
            thread_name_prefix="test-nested-unused",
        ) as borrowed:
            future = borrowed.submit(lambda: threading.current_thread().name)
            assert future is not None
            return outer_thread, future.result(timeout=1.0)

    try:
        future = executor.submit(nested_fetch)
        assert future is not None
        outer_thread, inner_thread = future.result(timeout=1.0)
        assert outer_thread.startswith("test-nested-spare")
        assert inner_thread.startswith("test-nested-spare")
        assert inner_thread != outer_thread
    finally:
        executor.stop()

    assert not any(thread.name.startswith("test-nested-") for thread in threading.enumerate())


def test_all_shared_pool_workers_can_borrow_without_waiting_on_their_own_queue() -> None:
    executor = BoundedExecutor(
        worker_count=2,
        queue_capacity=2,
        thread_name_prefix="test-nested-multi",
    )
    entered = threading.Barrier(2)
    assert executor.start() is True

    def nested_fetch() -> int:
        entered.wait(timeout=1.0)
        with borrow_executor(
            executor,
            worker_count=1,
            thread_name_prefix="test-nested-unused",
        ) as borrowed:
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
