from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Condition, Event

import pytest

from trader.recommendation.infra.market_data import announcement_request_gate
from trader.recommendation.infra.market_data.announcement_request_gate import CninfoDeadlineExceeded, CninfoRequestGate


def test_cninfo_request_gate_admits_waiters_before_repeated_request(monkeypatch) -> None:
    waiting = (Event(), Event())
    wait_count = 0

    class NotifyingCondition(Condition):
        def wait(self, timeout: float | None = None) -> bool:
            nonlocal wait_count
            if wait_count < len(waiting):
                waiting[wait_count].set()
                wait_count += 1
            return super().wait(timeout)

    monkeypatch.setattr(announcement_request_gate, "Condition", NotifyingCondition)
    gate = CninfoRequestGate(minimum_interval_seconds=0.0, monotonic=time.monotonic, sleeper=time.sleep)
    admitted: list[int] = []
    deadline = time.monotonic() + 5.0

    def enter(number: int) -> None:
        with gate.enter(deadline):
            admitted.append(number)

    with ThreadPoolExecutor(max_workers=2) as pool:
        with gate.enter(deadline):
            second = pool.submit(enter, 2)
            assert waiting[0].wait(timeout=2.0)
            third = pool.submit(enter, 3)
            assert waiting[1].wait(timeout=2.0)
        enter(1)
        second.result(timeout=2.0)
        third.result(timeout=2.0)

    assert admitted == [2, 3, 1]


def test_cninfo_expired_waiter_is_removed_without_blocking_next_request() -> None:
    gate = CninfoRequestGate(minimum_interval_seconds=0.0, monotonic=time.monotonic, sleeper=time.sleep)

    def expired_waiter() -> None:
        with pytest.raises(CninfoDeadlineExceeded), gate.enter(time.monotonic()):
            pytest.fail("expired waiter must not enter")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with gate.enter(time.monotonic() + 5.0):
            pool.submit(expired_waiter).result(timeout=2.0)
    with gate.enter(time.monotonic() + 5.0) as remaining:
        assert remaining > 0.0


def test_cninfo_gate_releases_lane_after_transport_failure() -> None:
    gate = CninfoRequestGate(minimum_interval_seconds=0.0, monotonic=time.monotonic, sleeper=time.sleep)
    with pytest.raises(OSError), gate.enter(time.monotonic() + 5.0):
        raise OSError("fixture transport failure")
    with gate.enter(time.monotonic() + 5.0) as remaining:
        assert remaining > 0.0
