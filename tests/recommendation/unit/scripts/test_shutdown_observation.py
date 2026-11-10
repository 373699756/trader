"""No active server: only explicitly owned child processes receive test signals."""

import json
import errno
import os
import signal
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.core.cli import Main as ToolMain
from scripts.core.process import RunBoundedCommand
from scripts.core.paths import PROJECT_ROOT
from scripts.recommendation.web.shutdown import ObserveShutdown, Run, ShutdownObservationError, ShutdownObservationOptions
from trader.infra.linux_process import LinuxProcessIdentity, ReadLinuxProcessIdentity
from trader.infra.process_family import ProcessFamily


_CHILD = """
import logging, signal, sys, threading
from trader.entrypoints.server import _queue_shutdown_report
from trader.infra.shutdown import ShutdownSignalController, RecordShutdownEvent, ShutdownReport, ShutdownStep
stopped, projected = threading.Event(), threading.Event()
class Handler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        if record.msg == 'shutdown completed: %s': projected.set()
logger = logging.getLogger('trader.entrypoints.server')
logger.addHandler(Handler(sys.stdout))
logger.setLevel(logging.WARNING)
controller = ShutdownSignalController(timeout_seconds=30, on_first_signal=lambda deadline: stopped.set(),
    protect_process_exit=True)
controller.install()
print('ready', flush=True)
stopped.wait()
deadline = controller.deadline
RecordShutdownEvent(deadline, 'return_ready')
_queue_shutdown_report(ShutdownReport.from_steps(deadline, (ShutdownStep('fixture', True, False),)), deadline)
assert projected.wait(3)
threading.Thread(target=lambda: threading.Event().wait(0.08), name='owned-exit-tail').start()
raise SystemExit(controller.exit_code)
"""


@contextmanager
def _child(tmp_path: Path, code: str = _CHILD) -> Iterator[tuple[subprocess.Popen[bytes], Path, int]]:
    log_path = tmp_path / "owned-child.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(
            (sys.executable, "-c", code), stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 8
            while "ready" not in log_path.read_text():
                assert process.poll() is None, log_path.read_text()
                assert time.monotonic() < deadline, "owned child did not become ready"
                time.sleep(0.01)
            identity = ReadLinuxProcessIdentity(process.pid)
            assert identity is not None
            yield process, log_path, identity.birth_ticks
        finally:
            # This group was created by this fixture. No unrelated target is touched.
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd required")


@pytest.fixture
def pidfd_python():
    executable = shutil.which("python3")
    assert executable is not None
    probe = subprocess.run((executable, "-c", "import os,signal; assert hasattr(os,'pidfd_open') and hasattr(signal,'pidfd_send_signal')"), check=False, timeout=3)
    if probe.returncode:
        pytest.skip("default python3 has no standard pidfd capability; no interpreter fallback")
    return executable


def _environment():
    return dict(os.environ, PYTHONPATH=os.pathsep.join((str(PROJECT_ROOT / "src"), str(PROJECT_ROOT))))


def _public_observation(executable, process, log, birth, *, send=False, seconds="5"):
    result = RunBoundedCommand((
        executable, str(PROJECT_ROOT / "scripts/tools.py"),
        "--business", "recommendation", "--module", "web", "--action", "shutdown",
        "--pid", str(process.pid), "--birth-ticks", str(birth), "--shutdown-log", str(log),
        "--observation-seconds", seconds, *(("--send-sigterm",) if send else ()),
    ), timeout=8, environment=_environment())
    return result.returncode, json.loads(result.stdout)["checks"][0]["summary"]


def test_explicit_sigterm_binds_send_handler_return_and_pidfd_exit(tmp_path, pidfd_python):
    with _child(tmp_path) as (process, log, birth):
        code, outcome = _public_observation(pidfd_python, process, log, birth, send=True)
        assert process.wait(timeout=3) == 143
    assert code == 0 and outcome["status"] == "passed"
    summary = outcome["summary"]
    assert summary["sigterm_sent"] and summary["timeline_complete"]
    assert summary["within_shutdown_limit"] and not summary["runtime_shutdown_acceptance"]
    assert summary["target_exit_code"] is None
    timeline = summary["timeline"]
    assert timeline["signal_send_started_monotonic"] <= timeline["python_handler_started_monotonic"]
    assert timeline["python_handler_started_monotonic"] <= timeline["business_return_ready_monotonic"]
    assert timeline["business_return_ready_monotonic"] <= timeline["pidfd_exit_observed_monotonic"]
    assert summary["durations"]["business_return_to_exit_observed_seconds"] > 0
    assert str(log) not in json.dumps(outcome)


def test_owned_family_supplies_real_exit_status_without_signing_a_fixture_as_runtime(tmp_path):
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        pytest.skip("current interpreter lacks pidfd")
    log = tmp_path / "owned-family.log"
    with log.open("wb") as output:
        family = ProcessFamily((sys.executable, "-c", _CHILD), stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 8
            while "ready" not in log.read_text():
                assert family.poll() is None and time.monotonic() < deadline
                time.sleep(0.01)
            identity = ReadLinuxProcessIdentity(family.pid)
            assert identity is not None
            result = ObserveShutdown(
                ShutdownObservationOptions(family.pid, identity.birth_ticks, log, 3, True), owned_process=family,
            )
            assert result.payload["status"] == "passed"
            summary = result.payload["summary"]
            assert summary["target_exit_code"] == 143
            assert summary["process_shutdown_acceptance"] is True
            assert summary["runtime_shutdown_acceptance"] is False
        finally:
            family.close()
            family.close_streams()


@pytest.mark.parametrize("send", (False, True))
def test_observation_timeout_never_force_kills_a_nonowned_target(tmp_path, pidfd_python, send):
    code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(30)"
    with _child(tmp_path, code) as (process, log, birth):
        result, outcome = _public_observation(pidfd_python, process, log, birth, send=send, seconds="0.05")
        assert result == 124 and outcome["status"] == "timed_out"
        assert process.poll() is None
    summary = outcome["summary"]
    assert summary["read_only"] is not send and summary["sigterm_sent"] is send
    if not send:
        assert summary["timeline"]["signal_send_started_monotonic"] is None
    assert summary["within_shutdown_limit"] is None


def test_wrong_birth_is_rejected_before_signalling(tmp_path):
    code = "import time; print('ready', flush=True); time.sleep(30)"
    with _child(tmp_path, code) as (process, log, birth):
        with pytest.raises(ShutdownObservationError, match="target_identity_unavailable"):
            ObserveShutdown(ShutdownObservationOptions(process.pid, birth + 1, log, 0.1, True))
        assert process.poll() is None


def test_fifo_log_is_rejected_without_waiting_for_a_writer_or_signalling(tmp_path):
    fifo = tmp_path / "not-a-regular-log"
    os.mkfifo(fifo)
    code = "import time; print('ready', flush=True); time.sleep(30)"
    with _child(tmp_path, code) as (process, _log, birth):
        result, outcome = _public_observation(sys.executable, process, fifo, birth, seconds="0.05")
        assert result == 1 and outcome["status"] == "failed"
        assert outcome["findings"] == ["shutdown_observation_regular_log_required"]
        assert process.poll() is None


def test_current_interpreter_missing_pidfd_is_explicit_unsupported(tmp_path):
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        pytest.skip("this interpreter exposes standard pidfds")
    code = "import time; print('ready', flush=True); time.sleep(30)"
    with _child(tmp_path, code) as (process, log, birth):
        result = Run("shutdown", [
            "--pid", str(process.pid), "--birth-ticks", str(birth), "--shutdown-log", str(log),
            "--send-sigterm",
        ])
        assert result.exit_code == 1 and result.payload["status"] == "failed"
        assert result.payload["summary"]["failure_category"] == "unsupported"
        assert process.poll() is None


def test_reaped_target_is_rejected_without_sending_a_new_signal(tmp_path):
    code = "import time; print('ready', flush=True); time.sleep(30)"
    with _child(tmp_path, code) as (process, log, birth):
        process.terminate()
        process.wait(timeout=3)
        with pytest.raises(ShutdownObservationError, match="target_identity_unavailable"):
            ObserveShutdown(ShutdownObservationOptions(process.pid, birth, log, 0.1, True))


def test_reused_identity_after_pidfd_open_is_rejected_and_descriptor_is_closed(tmp_path, monkeypatch):
    from scripts.recommendation.web import shutdown
    identity = LinuxProcessIdentity(os.getpid() + 1000, 1, "d50dbb32-ac67-4d74-99a9-7ad7db37c791")
    replacement = LinuxProcessIdentity(identity.pid, 2, identity.boot_id)
    identities = iter((identity, replacement))
    descriptor = os.open(os.devnull, os.O_RDONLY)
    monkeypatch.setattr(shutdown, "ReadLinuxProcessIdentity", lambda _pid: next(identities))
    monkeypatch.setattr(shutdown, "OpenLinuxProcessDescriptor", lambda _identity: descriptor)
    monkeypatch.setattr(shutdown, "SignalLinuxProcessDescriptor", lambda *_args: pytest.fail("replacement was signalled"))
    log = tmp_path / "unused.log"
    log.touch()
    with pytest.raises(ShutdownObservationError, match="target_changed_before_wait"):
        ObserveShutdown(ShutdownObservationOptions(identity.pid, identity.birth_ticks, log, 0.1, True))
    with pytest.raises(OSError) as captured:
        os.fstat(descriptor)
    assert captured.value.errno == errno.EBADF


@pytest.mark.parametrize(("error", "category"), (
    (errno.EPERM, "permission_denied"), (errno.ESRCH, "target_exited"), (errno.ENOSYS, "unsupported"),
))
def test_kernel_errors_keep_distinct_errno_and_never_fall_back_to_pid_signals(tmp_path, monkeypatch, error, category):
    from scripts.recommendation.web import shutdown

    def fail(_identity):
        raise OSError(error, "controlled kernel error")

    code = "import time; print('ready', flush=True); time.sleep(30)"
    with _child(tmp_path, code) as (process, log, birth):
        monkeypatch.setattr(shutdown, "OpenLinuxProcessDescriptor", fail)
        result = Run("shutdown", [
            "--pid", str(process.pid), "--birth-ticks", str(birth), "--shutdown-log", str(log), "--send-sigterm",
        ])
        assert result.payload["summary"]["failure_category"] == category
        assert result.payload["summary"]["failure_errno"] == error
        assert process.poll() is None


def test_public_help_parses_without_reading_files_or_signalling(capsys):
    with pytest.raises(SystemExit) as caught:
        ToolMain(["--business", "recommendation", "--module", "web", "--action", "shutdown", "--help"])
    assert caught.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--send-sigterm" in help_text and "default only observes" in help_text


@pytest.mark.parametrize("mismatch", ("pid", "birth_ticks", "boot_id"))
def test_unmatched_log_identity_cannot_fill_the_timeline(tmp_path, mismatch):
    from scripts.recommendation.web.shutdown import _read_events
    identity = ReadLinuxProcessIdentity(os.getpid())
    assert identity is not None
    value = {"pid": identity.pid, "birth_ticks": identity.birth_ticks, "boot_id": identity.boot_id}
    value[mismatch] = "foreign" if mismatch == "boot_id" else value[mismatch] + 1
    log = tmp_path / "foreign.log"
    log.write_text("shutdown completed: " + json.dumps({
        "process_identity": value, "started_at_monotonic": 10,
        "timeline": [{"name": "signal_received", "started_seconds": 0}, {"name": "return_ready", "started_seconds": 1}],
    }))
    with log.open("rb") as stream:
        result = _read_events(stream, 0, identity, 1)
    assert result.matched_records == 0 and result.handler_started is result.return_ready is None


def test_two_deadlines_for_one_identity_are_never_spliced_into_a_complete_axis(tmp_path):
    from scripts.recommendation.web.shutdown import _read_events
    identity = ReadLinuxProcessIdentity(os.getpid())
    assert identity is not None
    value = {"pid": identity.pid, "birth_ticks": identity.birth_ticks, "boot_id": identity.boot_id}
    records = [{"process_identity": value, "started_at_monotonic": origin, "timeline": [
        {"name": "signal_received", "started_seconds": 0}, {"name": "return_ready", "started_seconds": 1},
    ]} for origin in (10, 11)]
    log = tmp_path / "conflicting.log"
    log.write_text("\n".join("shutdown completed: " + json.dumps(record) for record in records))
    with log.open("rb") as stream:
        observed = _read_events(stream, 0, identity, 1)
    assert observed.conflicting and observed.matched_records == 2


def _read_timeline(tmp_path, spans):
    from scripts.recommendation.web.shutdown import _read_events

    identity = LinuxProcessIdentity(7412, 1234, "d50dbb32-ac67-4d74-99a9-7ad7db37c791")
    record = {
        "process_identity": {"pid": identity.pid, "birth_ticks": identity.birth_ticks, "boot_id": identity.boot_id},
        "started_at_monotonic": 10,
        "timeline": spans,
        "report": {"completed": True, "forced": False},
    }
    log = tmp_path / "timeline.log"
    log.write_text("shutdown completed: " + json.dumps(record))
    with log.open("rb") as stream:
        return identity, _read_events(stream, 0, identity, 9)


@pytest.mark.parametrize("offset", (0, 0.000025, 0.000067, 0.125))
def test_nonnegative_signal_event_offsets_use_the_same_timeline_origin(tmp_path, offset):
    from scripts.recommendation.web.shutdown import _Times, _outcome

    identity, observed = _read_timeline(tmp_path, [
        {"name": "signal_received", "started_seconds": offset},
        {"name": "return_ready", "started_seconds": 1},
    ])
    assert observed.handler_started == 10 + offset
    assert observed.return_ready == 11 and not observed.conflicting
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(9, 9.5, 9.6, 9.7, 12, 12), observed, cancelled=False,
    )
    assert result.exit_code == 0 and result.payload["status"] == "passed"
    assert result.payload["summary"]["timeline_complete"] is True
    assert result.payload["summary"]["durations"]["signal_to_handler_seconds"] == pytest.approx(0.5 + offset, abs=1e-12)
    assert result.payload["summary"]["shutdown_limit_seconds"] == 30


@pytest.mark.parametrize("offset", (-0.000001, True, "0.000025", None, float("nan"), float("inf")))
def test_invalid_signal_event_offsets_cannot_supply_a_handler_time(tmp_path, offset):
    from scripts.recommendation.web.shutdown import _Times, _outcome

    identity, observed = _read_timeline(tmp_path, [
        {"name": "signal_received", "started_seconds": offset},
        {"name": "return_ready", "started_seconds": 1},
    ])
    assert observed.handler_started is None
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(9, 9.5, 9.6, 9.7, 12, 12), observed, cancelled=False,
    )
    assert result.payload["status"] == "degraded"
    assert result.payload["summary"]["timeline_complete"] is False


def test_conflicting_signal_event_offsets_are_not_merged_into_one_handler(tmp_path):
    from scripts.recommendation.web.shutdown import _Times, _outcome

    identity, observed = _read_timeline(tmp_path, [
        {"name": "signal_received", "started_seconds": 0.000025},
        {"name": "signal_received", "started_seconds": 0.000067},
        {"name": "return_ready", "started_seconds": 1},
    ])
    assert observed.conflicting and observed.handler_started is None
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(9, 9.5, 9.6, 9.7, 12, 12), observed, cancelled=False,
    )
    assert result.exit_code == 1 and result.payload["status"] == "failed"
    assert result.payload["summary"]["timeline_complete"] is False


def test_signal_event_after_return_is_a_conflict_even_with_one_origin(tmp_path):
    from scripts.recommendation.web.shutdown import _Times, _outcome

    identity, observed = _read_timeline(tmp_path, [
        {"name": "signal_received", "started_seconds": 2},
        {"name": "return_ready", "started_seconds": 1},
    ])
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(9, 9.5, 9.6, 9.7, 13, 13), observed, cancelled=False,
    )
    assert result.exit_code == 1 and result.payload["status"] == "failed"
    assert result.payload["summary"]["timeline_complete"] is False


def test_observed_exit_over_the_unchanged_gate_never_passes():
    from scripts.recommendation.web.shutdown import _LogObservation, _Times, _outcome
    identity = LinuxProcessIdentity(7412, 1234, "d50dbb32-ac67-4d74-99a9-7ad7db37c791")
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(1, 2, 2.01, 2.02, 35, 35),
        _LogObservation(3, 4, True, False, matched_records=1), cancelled=False,
    )
    assert result.exit_code == 1 and result.payload["status"] == "failed"
    assert result.payload["summary"]["shutdown_limit_seconds"] == 30
    assert result.payload["summary"]["within_shutdown_limit"] is False
    assert result.payload["summary"]["runtime_shutdown_acceptance"] is False


@pytest.mark.parametrize("exit_code", [0, 2, -15, 143])
def test_owned_child_exit_semantics_are_required_even_when_cleanup_is_fast(exit_code):
    from scripts.recommendation.web.shutdown import _LogObservation, _Times, _outcome

    identity = LinuxProcessIdentity(7412, 1234, "d50dbb32-ac67-4d74-99a9-7ad7db37c791")
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(1, 2, 2.01, 2.02, 5, 5), _LogObservation(3, 4, True, False, matched_records=1),
        cancelled=False, target_exit_code=exit_code,
    )
    assert result.payload["summary"]["process_shutdown_acceptance"] is (exit_code == 143)
    assert result.payload["status"] == ("passed" if exit_code == 143 else "failed")


def test_handler_before_this_signal_cannot_pass_as_a_complete_timeline():
    from scripts.recommendation.web.shutdown import _LogObservation, _Times, _outcome
    identity = LinuxProcessIdentity(7412, 1234, "d50dbb32-ac67-4d74-99a9-7ad7db37c791")
    result = _outcome(
        ShutdownObservationOptions(identity.pid, identity.birth_ticks, Path("unused"), 40, True), identity,
        _Times(1, 2, 2.01, 2.02, 5, 5),
        _LogObservation(1.5, 4, True, False, matched_records=1), cancelled=False,
    )
    assert result.exit_code == 1 and result.payload["status"] == "failed"
    assert result.payload["summary"]["timeline_complete"] is False
    assert "shutdown_observation_timeline_conflicting_or_incomplete" in result.payload["findings"]


def test_outer_timeout_reclaims_only_its_owned_observer_and_descendant_group(tmp_path, pidfd_python):
    marker, log = tmp_path / "owned-pids.json", tmp_path / "target.log"
    log.touch()
    code = """
import json, os, signal, subprocess, sys, time
from pathlib import Path
from scripts.recommendation.web.shutdown import ObserveShutdown, ShutdownObservationOptions
from trader.infra.linux_process import ReadLinuxProcessIdentity
target = subprocess.Popen((sys.executable, '-c', 'import signal,time; signal.signal(signal.SIGINT, signal.SIG_IGN); time.sleep(30)'))
Path(sys.argv[1]).write_text(json.dumps({'target': target.pid, 'observer': os.getpid()}))
identity = ReadLinuxProcessIdentity(target.pid)
ObserveShutdown(ShutdownObservationOptions(target.pid, identity.birth_ticks, Path(sys.argv[2]), 30))
"""
    with pytest.raises(subprocess.TimeoutExpired):
        # Includes a cold interpreter and imports before the marker exists.
        # Keep the child's 30-second wait longer than this separate fixture budget.
        RunBoundedCommand((pidfd_python, "-c", code, str(marker), str(log)), timeout=5, environment=_environment())
    pids = json.loads(marker.read_text())
    deadline = time.monotonic() + 3
    while any(ReadLinuxProcessIdentity(pid) is not None for pid in pids.values()):
        assert time.monotonic() < deadline, "owned timeout group remained running"
        time.sleep(0.01)
