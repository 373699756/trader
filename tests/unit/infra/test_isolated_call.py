"""Real child-process execution, timeout, cancellation and cleanup."""

import math
import multiprocessing
import threading
import time
from concurrent.futures import CancelledError
from functools import partial

import pytest

from trader.infra.isolated_call import RunIsolatedCall


def test_isolated_success_and_failure_reap_children():
    before = {child.pid for child in multiprocessing.active_children()}
    assert RunIsolatedCall(partial(math.sqrt, 9), 5) == 3
    with pytest.raises(RuntimeError, match="sdk_failed_ValueError"):
        RunIsolatedCall(partial(int, "private invalid input"), 5)
    assert {child.pid for child in multiprocessing.active_children()} == before


@pytest.mark.parametrize("cancel", [False, True])
def test_blocked_sdk_is_terminated_and_reaped(cancel):
    before = {child.pid for child in multiprocessing.active_children()}
    event = threading.Event()
    timer = threading.Timer(0.15, event.set)
    if cancel:
        timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(CancelledError if cancel else TimeoutError):
            RunIsolatedCall(partial(time.sleep, 30), 5 if cancel else 0.15, cancel_requested=event.is_set)
    finally:
        if cancel:
            timer.join(timeout=1)
    assert time.monotonic() - started < 2
    assert {child.pid for child in multiprocessing.active_children()} == before
