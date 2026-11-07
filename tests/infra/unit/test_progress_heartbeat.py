"""Progress heartbeat must finish before its owning download scope closes."""

from threading import Event, current_thread

from trader.infra.progress_heartbeat import ProgressHeartbeat
from trader.infra.shutdown import ShutdownDeadline


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


def test_explicit_deadline_reports_a_blocked_callback_without_hiding_the_thread():
    blocked = Event()
    release = Event()
    caller = current_thread()

    def report(_message):
        if current_thread() is not caller:
            blocked.set()
            assert release.wait(2)

    progress = ProgressHeartbeat(report, interval_seconds=0.01)
    progress.__enter__()
    try:
        progress("fixture")
        assert blocked.wait(2)
        deadline = ShutdownDeadline.start(0.02)
        result = progress.stop(deadline)
        assert not result.completed and result.timed_out
        assert progress._thread is not None and progress._thread.is_alive()
    finally:
        release.set()
        assert progress.stop(ShutdownDeadline.start(2)).completed
