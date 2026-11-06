"""Use real subprocesses to prove that even an uncooperative driver is reaped."""

import multiprocessing
import os
import threading
import time
from pathlib import Path

import pytest

from trader.infra.http_contracts import HttpCancelled, HttpRequest, HttpResponse, HttpTimeout
from trader.infra.isolated_http import IsolatedHttpExecutor


class _BlockedTransport:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, request):
        Path(request.url).write_text("entered", encoding="utf-8")
        time.sleep(30)
        return HttpResponse(200, b"late")


class _ReusableTransport(_BlockedTransport):
    def __init__(self):
        self.requests = 0

    def execute(self, request):
        self.requests += 1
        if request.url == "block":
            time.sleep(30)
        return HttpResponse(200, f"{os.getpid()}:{self.requests}".encode())


def test_healthy_requests_reuse_process_and_transport_but_timeout_replaces_worker():
    before = {process.pid for process in multiprocessing.active_children()}
    executor = IsolatedHttpExecutor(worker_count=1, transport_factory=_ReusableTransport)
    executor.start()
    try:
        first = executor.execute(HttpRequest("GET", "first", 3)).content.decode().split(":")
        second = executor.execute(HttpRequest("GET", "second", 3)).content.decode().split(":")
        assert first[0] == second[0]
        assert (first[1], second[1]) == ("1", "2")
        with pytest.raises(HttpTimeout):
            executor.execute(HttpRequest("GET", "block", 0.1))
        third = executor.execute(HttpRequest("GET", "third", 3)).content.decode().split(":")
        assert third[0] != first[0]
        assert third[1] == "1"
    finally:
        assert executor.stop().completed
    assert {process.pid for process in multiprocessing.active_children()} == before


def test_capacity_wait_uses_request_deadline_and_stop_reaps_active_worker(tmp_path):
    before = {process.pid for process in multiprocessing.active_children()}
    marker = tmp_path / "entered"
    executor = IsolatedHttpExecutor(worker_count=1, transport_factory=_BlockedTransport)
    executor.start()
    failures = []

    def run():
        try:
            executor.execute(HttpRequest("GET", str(marker), 10))
        except HttpCancelled as exc:
            failures.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        with pytest.raises(HttpTimeout):
            executor.execute(HttpRequest("GET", "queued", 0.1))
        executor.stop(wait=False)
        assert executor.stop().completed
    finally:
        executor.stop()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert len(failures) == 1
    assert {process.pid for process in multiprocessing.active_children()} == before


@pytest.mark.parametrize("cancel", [False, True])
def test_uncooperative_http_is_terminated_and_reaped(cancel, tmp_path):
    before = {process.pid for process in multiprocessing.active_children()}
    cancelled = threading.Event()
    marker = tmp_path / "http-entered"

    def cancel_after_entry():
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            cancelled.wait(0.01)
        cancelled.set()

    timer = threading.Thread(target=cancel_after_entry)
    if cancel:
        timer.start()
    started = time.monotonic()
    executor = IsolatedHttpExecutor(worker_count=1, transport_factory=_BlockedTransport)
    executor.start()
    try:
        with pytest.raises(HttpCancelled if cancel else HttpTimeout):
            executor.execute(
                HttpRequest("GET", str(marker), 10 if cancel else 1.0, cancel_requested=cancelled.is_set),
            )
    finally:
        executor.stop()
        if cancel:
            timer.join()
    assert time.monotonic() - started < 2
    assert marker.read_text(encoding="utf-8") == "entered"
    assert {process.pid for process in multiprocessing.active_children()} == before
