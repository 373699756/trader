import signal
from threading import Event

import pytest

from trader.infra.signal_cancellation import ManualCancellation


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
def test_manual_cancellation_stops_dispatch_then_forces_exit_and_restores_handlers(signum, capfd):
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    forced = []
    with ManualCancellation(force_exit=forced.append) as cancelled:
        handler = signal.getsignal(signum)
        assert callable(handler)
        assert not cancelled.is_set()
        handler(signum, None)
        assert cancelled.is_set()
        assert "正在取消" in capfd.readouterr().err
        handler(signum, None)
        assert forced == [130]
    assert {value: signal.getsignal(value) for value in previous} == previous


def test_watchdog_covers_blocked_cleanup():
    forced = Event()
    codes = []

    def stop(code):
        codes.append(code)
        forced.set()

    with ManualCancellation(timeout_seconds=0.05, force_exit=stop):
        signal.raise_signal(signal.SIGINT)
        assert forced.wait(2)
    assert codes == [130]


def test_completed_cleanup_disarms_watchdog():
    forced = Event()
    with ManualCancellation(timeout_seconds=0.1, force_exit=lambda _: forced.set()):
        signal.raise_signal(signal.SIGINT)
    assert not forced.wait(0.2)
