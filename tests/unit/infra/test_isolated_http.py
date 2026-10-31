"""Use real subprocesses to prove that even an uncooperative driver is reaped."""

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from trader.infra.http_contracts import HttpCancelled, HttpRequest, HttpResponse, HttpTimeout
from trader.infra.isolated_http import ExecuteIsolatedHttp


class _BlockedTransport:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, request):
        Path(request.url).write_text("entered", encoding="utf-8")
        time.sleep(30)
        return HttpResponse(200, b"late")


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
    try:
        with pytest.raises(HttpCancelled if cancel else HttpTimeout):
            ExecuteIsolatedHttp(
                HttpRequest("GET", str(marker), 10 if cancel else 1.0, cancel_requested=cancelled.is_set),
                transport_factory=_BlockedTransport,
            )
    finally:
        if cancel:
            timer.join()
    assert time.monotonic() - started < 2
    assert marker.read_text(encoding="utf-8") == "entered"
    assert {process.pid for process in multiprocessing.active_children()} == before
