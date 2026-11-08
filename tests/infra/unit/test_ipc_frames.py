"""Actual partial frames and blocked writes obey the original deadline."""

import multiprocessing
import struct
import threading
import time
from concurrent.futures import CancelledError
from functools import partial
from pathlib import Path

import pytest

from trader.infra import isolated_call, isolated_http
from trader.infra.http_contracts import HttpCancelled, HttpError, HttpRequest, HttpTimeout


def _fault(connection, mode):
    if mode == "header":
        connection.sendall(b"\x00\x00")
    elif mode in {"payload", "exit"}:
        connection.sendall(struct.pack("!Q", 1024) + b"partial")
    if mode != "exit":
        time.sleep(30)
    connection.close()


def _http_fault(connection, _factory, *, marker, mode):
    Path(marker).touch()
    _fault(connection, mode)


def _sdk_fault(_operation, connection, *, marker, mode):
    Path(marker).touch()
    _fault(connection, mode)


@pytest.mark.parametrize(("kind", "mode"), (
    *(("http", mode) for mode in ("header", "payload", "exit", "send")),
    *(("sdk", mode) for mode in ("header", "payload", "exit")),
))
@pytest.mark.parametrize("cancel", (False, True))
def test_partial_frames_blocked_sends_and_peer_exit_are_supervised(tmp_path, monkeypatch, kind, mode, cancel):
    before = {child.pid for child in multiprocessing.active_children()}
    marker = tmp_path / "entered"
    cancelled = threading.Event()

    def request_cancel():
        limit = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < limit:
            cancelled.wait(0.01)
        cancelled.set()

    timer = threading.Thread(target=request_cancel)
    if cancel:
        timer.start()
    started = time.monotonic()
    timeout = 5 if cancel else 1.0
    expected = (HttpError if kind == "http" else RuntimeError) if mode == "exit" else (
        (HttpCancelled if cancel else HttpTimeout) if kind == "http" else (CancelledError if cancel else TimeoutError)
    )
    try:
        with pytest.raises(expected):
            if kind == "http":
                monkeypatch.setattr(isolated_http, "_serve", partial(_http_fault, marker=str(marker), mode=mode))
                executor = isolated_http.IsolatedHttpExecutor(worker_count=1, transport_factory=None)
                executor.start()
                try:
                    executor.execute(HttpRequest(
                        "POST", "fixture", timeout, body=b"x" * (8 * 1024 * 1024) if mode == "send" else None,
                        cancel_requested=cancelled.is_set,
                    ))
                finally:
                    assert executor.stop().completed
            else:
                monkeypatch.setattr(isolated_call, "_invoke", partial(_sdk_fault, marker=str(marker), mode=mode))
                isolated_call.RunIsolatedCall(bytes, timeout, cancel_requested=cancelled.is_set)
    finally:
        if cancel:
            timer.join(timeout=5)
    assert time.monotonic() - started < 2
    assert {child.pid for child in multiprocessing.active_children()} == before
