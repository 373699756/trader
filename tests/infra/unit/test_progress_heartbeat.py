"""Progress heartbeat must finish before its owning download scope closes."""

from threading import Event, current_thread

from trader.infra.progress_heartbeat import ProgressHeartbeat


def test_heartbeat_repeats_latest_progress_and_joins_on_exit():
    emitted = Event()
    threads = []
    messages = []
    caller = current_thread()

    def report(message):
        messages.append(message)
        if current_thread() is not caller:
            threads.append(current_thread())
            emitted.set()

    with ProgressHeartbeat(report, interval_seconds=0.01) as progress:
        progress("等待供应商")
        assert emitted.wait(2)
    assert messages == ["等待供应商"] * len(messages)
    assert len(messages) >= 2
    assert all(not thread.is_alive() for thread in threads)
