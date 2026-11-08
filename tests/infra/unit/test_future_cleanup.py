"""A deadline may return before cleanup, while the real resource stays owned."""

from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from trader.infra.future_cleanup import CloseAfterFutures


@pytest.mark.parametrize("failure", (False, True))
def test_cleanup_cannot_be_cancelled_and_waits_for_all_real_work(failure):
    first, last = Future(), Future()
    entered, release = Event(), Event()
    calls = []
    cause = OSError("controlled close failure")

    def close():
        calls.append("close")
        entered.set()
        assert release.wait(3)
        if failure:
            raise cause

    completion = CloseAfterFutures(close, (first, last))
    assert not completion.cancel()
    first.set_exception(ValueError("earlier work failure"))
    assert not completion.done() and not entered.is_set()
    with ThreadPoolExecutor(max_workers=1) as worker:
        finishing = worker.submit(last.set_result, None)
        try:
            assert entered.wait(3)
            assert not completion.done() and not completion.cancel()
        finally:
            release.set()
        finishing.result(timeout=3)
    assert calls == ["close"]
    if failure:
        assert completion.exception(timeout=3) is cause
    else:
        assert completion.result(timeout=3) is None
