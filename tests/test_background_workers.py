import threading

import pytest

from stock_analyzer.background_workers import BackgroundWorkerGroup


def _waiting_starter(started: threading.Event):
    def start(stop_event: threading.Event) -> threading.Thread:
        def run() -> None:
            started.set()
            stop_event.wait()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    return start


def test_worker_group_starts_once_stops_and_restarts():
    started = threading.Event()
    group = BackgroundWorkerGroup([_waiting_starter(started)])

    assert group.start() is True
    assert started.wait(1.0)
    assert group.start() is False
    assert group.status()["running_workers"] == 1

    group.stop(timeout_seconds=1.0)
    assert group.status()["running_workers"] == 0

    started.clear()
    assert group.start() is True
    assert started.wait(1.0)
    group.stop(timeout_seconds=1.0)
    assert group.status()["running_workers"] == 0


def test_worker_group_stops_started_threads_when_later_starter_fails():
    started = threading.Event()
    first_starter = _waiting_starter(started)

    def failing_starter(stop_event: threading.Event) -> threading.Thread:
        raise RuntimeError("starter failed")

    group = BackgroundWorkerGroup([first_starter, failing_starter])

    with pytest.raises(RuntimeError, match="starter failed"):
        group.start()

    assert started.wait(1.0)
    assert group.status()["running_workers"] == 0
    assert group.status()["stop_requested"] is True
