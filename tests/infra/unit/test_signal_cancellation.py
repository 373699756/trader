import signal
import threading
from threading import Event

import pytest

from trader.infra.signal_cancellation import ManualCancellation
from trader.infra.shutdown import ShutdownSignalController, ShutdownStep, ShutdownWatchdogStartError


@pytest.mark.parametrize("signum", (signal.SIGINT, signal.SIGTERM))
def test_manual_cancellation_stops_dispatch_then_forces_exit_and_restores_handlers(signum, capfd):
    previous = {value: signal.getsignal(value) for value in (signal.SIGINT, signal.SIGTERM)}
    forced = []
    with ManualCancellation(timeout_seconds=30.0, force_exit=forced.append) as cancelled:
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


def test_manual_cleanup_reuses_the_signal_deadline_without_resetting_its_budget(monkeypatch):
    from trader.infra import signal_cancellation

    clock = [100.0]
    controllers = []

    def controller(**options):
        created = ShutdownSignalController(**options, monotonic=lambda: clock[0])
        controllers.append(created)
        return created

    monkeypatch.setattr(signal_cancellation, "ShutdownSignalController", controller)
    with ManualCancellation(timeout_seconds=30.0, force_exit=lambda _: pytest.fail("unexpected forced exit")) as cancelled:
        assert isinstance(cancelled, Event)
        signal.raise_signal(signal.SIGTERM)
        original = controllers[0].deadline
        clock[0] += 17.0
        assert cancelled.shutdown_deadline() is original
        assert cancelled.shutdown_deadline().remaining_seconds() == 13.0
        cancelled.record_shutdown(ShutdownStep("fixture", True, False))


def test_first_signal_during_normal_cleanup_keeps_its_deadline_and_only_second_signal_forces_exit():
    forced = []
    with ManualCancellation(timeout_seconds=30.0, force_exit=forced.append) as cancelled:
        deadline = cancelled.shutdown_deadline()
        assert cancelled.shutdown_deadline() is deadline and not cancelled.is_set()
        signal.raise_signal(signal.SIGINT)
        assert cancelled.is_set() and forced == []
        assert cancelled.shutdown_deadline() is deadline
        signal.raise_signal(signal.SIGINT)
        assert forced == [130]
        cancelled.record_shutdown(ShutdownStep("fixture", True, False))


@pytest.mark.parametrize("signalled", (False, True))
def test_incomplete_cleanup_keeps_the_original_watchdog_armed_after_scope_exit(signalled):
    forced = Event()
    codes = []

    def stop(code):
        codes.append(code)
        forced.set()

    with ManualCancellation(timeout_seconds=0.05, force_exit=stop) as cancelled:
        if signalled:
            signal.raise_signal(signal.SIGTERM)
        cancelled.shutdown_deadline()
        cancelled.record_shutdown(ShutdownStep("fixture", False, False))
    assert forced.wait(2)
    assert codes == [130 if signalled else 2]


@pytest.mark.parametrize("reported", (False, True))
def test_unexpected_cleanup_exit_keeps_watchdog_and_cannot_clear_an_earlier_failure(reported):
    forced = Event()
    codes = []
    failure = SystemExit(7)

    def stop(code):
        codes.append(code)
        forced.set()

    with pytest.raises(SystemExit) as raised:
        with ManualCancellation(timeout_seconds=0.05, force_exit=stop) as cancelled:
            deadline = cancelled.shutdown_deadline()
            if reported:
                cancelled.record_shutdown(ShutdownStep("first", False, False))
                cancelled.record_shutdown(ShutdownStep("second", True, False))
            assert not cancelled.shutdown_completed
            assert cancelled.shutdown_deadline() is deadline
            assert not cancelled.shutdown_completed
            raise failure
    assert raised.value is failure
    assert forced.wait(2)
    assert codes == [2]


def test_reading_the_shared_deadline_after_a_complete_report_does_not_rearm_cleanup():
    forced = Event()
    with ManualCancellation(timeout_seconds=0.05, force_exit=lambda _: forced.set()) as cancelled:
        deadline = cancelled.shutdown_deadline()
        cancelled.record_shutdown(ShutdownStep("fixture", True, False))
        assert cancelled.shutdown_deadline() is deadline
        assert cancelled.shutdown_completed
    assert not forced.wait(0.2)


def test_watchdog_start_failure_preserves_the_deadline_cause_and_allows_an_explicit_retry(monkeypatch):
    failure = RuntimeError("private thread creation detail")
    controller = ShutdownSignalController(timeout_seconds=30, on_first_signal=lambda _deadline: None)

    def fail_start(_thread):
        raise failure

    with monkeypatch.context() as patches:
        patches.setattr(threading.Thread, "start", fail_start)
        with pytest.raises(ShutdownWatchdogStartError) as raised:
            controller.begin_shutdown()
        assert raised.value.__cause__ is failure
        assert raised.value.deadline is controller.deadline
        assert controller.watchdog_failure is raised.value
        assert not controller.watchdog_started
    try:
        assert controller.begin_shutdown() is raised.value.deadline
        assert controller.watchdog_started
        assert controller.watchdog_failure is raised.value
    finally:
        controller.mark_completed()


@pytest.mark.parametrize("completed", (False, True))
def test_scope_keeps_the_failed_watchdog_budget_and_bounds_incomplete_resources(monkeypatch, completed):
    forced = []

    def fail_start(_thread):
        raise RuntimeError("private watchdog failure")

    monkeypatch.setattr(threading.Thread, "start", fail_start)
    with ManualCancellation(timeout_seconds=30, force_exit=forced.append) as cancelled:
        deadline = cancelled.shutdown_deadline()
        assert cancelled.watchdog_failure is not None
        assert cancelled.watchdog_failure.deadline is deadline
        assert not cancelled.watchdog_started
        cancelled.record_shutdown(ShutdownStep("fixture", completed, False))
    assert forced == ([] if completed else [2])


def test_signal_recorded_before_event_publication_still_forces_the_cancellation_code(monkeypatch):
    forced = []
    start_thread = threading.Thread.start

    def start(thread):
        if thread.name == "trader-shutdown-watchdog":
            thread.run()
            return None
        return start_thread(thread)

    monkeypatch.setattr(threading.Thread, "start", start)
    with ManualCancellation(timeout_seconds=0, force_exit=lambda code: forced.append((code, cancelled.is_set()))) as cancelled:
        signal.raise_signal(signal.SIGTERM)
        assert cancelled.is_set()
    assert forced == [(130, False)]
