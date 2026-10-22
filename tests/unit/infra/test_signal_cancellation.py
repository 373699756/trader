import signal

import pytest

from trader.infra.signal_cancellation import ManualCancellation


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
def test_manual_cancellation_stops_dispatch_then_interrupts_and_restores_handlers(signum):
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    with ManualCancellation() as cancelled:
        handler = signal.getsignal(signum)
        assert callable(handler)
        assert not cancelled.is_set()
        handler(signum, None)
        assert cancelled.is_set()
        with pytest.raises(KeyboardInterrupt):
            handler(signum, None)
    assert {value: signal.getsignal(value) for value in previous} == previous
