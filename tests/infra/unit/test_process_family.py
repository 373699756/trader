"""Owned process identities remain pinned through family cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.infra import process_family
from trader.infra.process_family import ProcessFamily, ProcessFamilyCapabilityError


@pytest.mark.skipif(os.name != "posix", reason="POSIX non-reaping child observation")
def test_completed_leader_is_not_reaped_until_every_group_signal_has_been_sent(monkeypatch):
    family = ProcessFamily((sys.executable, "-c", "raise SystemExit(7)"))
    signals = []
    original = os.killpg

    def signal_owned_group(pid, signum):
        observed = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        assert observed is not None and observed.si_pid == family.pid
        assert observed.si_status == 7
        signals.append(signum)
        original(pid, signum)

    try:
        assert family.wait(5) == 7
        assert family.poll() == 7
        monkeypatch.setattr(process_family.os, "killpg", signal_owned_group)
        family.close()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert family.returncode == 7
        with pytest.raises(ChildProcessError):
            os.waitid(os.P_PID, family.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        family.close()
        assert len(signals) == 2
    finally:
        family.close()
        family.close_streams()


@pytest.mark.skipif(os.name != "posix", reason="POSIX external reaper boundary")
def test_external_reap_invalidates_ownership_before_any_group_signal(monkeypatch):
    family = ProcessFamily((sys.executable, "-c", "pass"))
    assert family.wait(5) == 0
    os.waitpid(family.pid, 0)
    monkeypatch.setattr(process_family.os, "killpg", lambda *_: pytest.fail("released PID was signalled"))
    try:
        with pytest.raises(RuntimeError, match="process_family_leader_identity_lost"):
            family.close()
    finally:
        family.close_streams()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux descendant observation")
def test_exited_leader_still_owns_and_cleans_its_living_descendant(tmp_path):
    marker = tmp_path / "descendant"
    child = "import os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    leader = "import subprocess,sys; subprocess.Popen((sys.executable,'-c',sys.argv[1],sys.argv[2]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    family = ProcessFamily((sys.executable, "-c", leader, child, str(marker)))
    try:
        assert family.wait(5) == 0
        deadline = time.monotonic() + 5
        while not marker.exists():
            assert time.monotonic() < deadline, "descendant did not become ready"
            time.sleep(0.01)
        pid = int(marker.read_text())
        family.close()
        status = Path(f"/proc/{pid}/stat")
        deadline = time.monotonic() + 3
        while status.exists() and status.read_text().split()[2] != "Z":
            assert time.monotonic() < deadline, "owned descendant remains alive"
            time.sleep(0.01)
    finally:
        family.close()
        family.close_streams()


def test_missing_nonreaping_capability_is_explicit_before_launch(monkeypatch):
    monkeypatch.setattr(process_family, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(process_family.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("unsupported child launched"))
    with pytest.raises(ProcessFamilyCapabilityError, match="nonreaping_wait_unavailable"):
        ProcessFamily(("unused",))


def test_windows_gate_worker_cannot_launch_a_tool_before_assignment_permission(tmp_path):
    marker = tmp_path / "started"
    with subprocess.Popen(
        (sys.executable, "-m", "trader.infra.process_family", sys.executable,
         "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as worker:
        with pytest.raises(subprocess.TimeoutExpired):
            worker.wait(timeout=0.2)
        assert not marker.exists()
        worker.communicate(input=b"1", timeout=5)
        assert worker.returncode == 0 and marker.read_text() == "started"


@pytest.mark.parametrize("assign_ok", (True, False))
def test_windows_job_binds_the_created_handle_and_kills_descendants_before_close(monkeypatch, assign_ok):
    calls = []

    class Api:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def __call__(self, *args):
            calls.append((self.name, args))
            if self.name == "limits":
                assert args[1] == 9 and args[2]._obj.basic.flags == 0x2000
            return self.result

    kernel = SimpleNamespace(
        CreateJobObjectW=Api("create", 11), SetInformationJobObject=Api("limits", 1),
        AssignProcessToJobObject=Api("assign", int(assign_ok)), CloseHandle=Api("close", 1),
        TerminateJobObject=Api("terminate", 1), QueryInformationJobObject=Api("query", 1),
    )
    monkeypatch.setattr(process_family.ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(process_family.ctypes, "WinError", lambda code: OSError(code, "job failed"), raising=False)
    monkeypatch.setattr(process_family.ctypes, "get_last_error", lambda: 5, raising=False)
    if not assign_ok:
        with pytest.raises(OSError):
            process_family._WindowsJob(123)
    else:
        job = process_family._WindowsJob(123)
        job.close()
        job.close()
        assert calls[-1] == ("close", (11,))
    assert ("assign", (11, 123)) in calls and ("close", (11,)) in calls


@pytest.mark.parametrize("assignment_fails", (False, True))
def test_windows_gate_release_follows_handle_assignment_and_startup_failure_is_reaped(monkeypatch, assignment_fails):
    calls = []

    class Gate:
        def write(self, value):
            assert "assigned" in calls and not assignment_fails
            calls.append(("release", value))

        def close(self):
            calls.append("gate_closed")

    class Process:
        pid, _handle, returncode = 123, 456, None
        stdin, stdout, stderr = Gate(), None, None

        def kill(self):
            calls.append("kill")

        def wait(self, **_kwargs):
            calls.append("wait")
            self.returncode = 0
            return 0

    process = Process()

    def launch(argv, **kwargs):
        assert argv[:3] == (sys.executable, "-m", "trader.infra.process_family")
        assert kwargs["stdin"] == subprocess.PIPE
        return process

    class Job:
        def __init__(self, handle):
            assert handle == 456
            if assignment_fails:
                raise OSError("controlled assignment failure")
            calls.append("assigned")

        def close(self):
            calls.append("job_closed")

    monkeypatch.setattr(process_family, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(process_family.subprocess, "Popen", launch)
    monkeypatch.setattr(process_family, "_WindowsJob", Job)
    if assignment_fails:
        with pytest.raises(OSError, match="controlled assignment failure"):
            ProcessFamily(("tool",))
        assert calls[:2] == ["kill", "wait"]
        assert ("release", b"1") not in calls
    else:
        family = ProcessFamily(("tool",))
        family.close()
        assert calls[:2] == ["assigned", ("release", b"1")]
        assert calls[-2:] == ["job_closed", "wait"]
