"""Real bounded pipes and simulated Windows job lifetime contracts."""

import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from trader.download.infra import company_document_process as processes


def test_windows_worker_waits_for_parent_permission_before_executing(tmp_path):
    marker = tmp_path / "started"
    with subprocess.Popen(
        (sys.executable, "-m", "trader.download.infra.company_document_process", sys.executable,
         "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started')", str(marker)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    ) as worker:
        with pytest.raises(subprocess.TimeoutExpired):
            worker.wait(timeout=0.2)
        assert not marker.exists()
        worker.communicate(input=b"1", timeout=5)
        assert worker.returncode == 0 and marker.read_text() == "started"


def test_windows_gated_capture_assigns_before_releasing_tool(monkeypatch, tmp_path):
    marker = tmp_path / "started"
    assigned = []

    class Family:
        def __init__(self, process):
            self.process = process
            assert not marker.exists()
            assigned.append(process.pid)

        def close(self):
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2)

    monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt", environ=os.environ))
    monkeypatch.setattr(processes, "DocumentProcessFamily", Family)
    code, output = processes.CaptureDocumentTool(
        (sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('started'); print('ready')", str(marker)),
        maximum_bytes=100, check=lambda: None,
    )
    assert code == 0 and output == b"ready\n" and assigned and marker.exists()


def test_output_limit_stops_a_continuously_writing_tool():
    started = time.monotonic()

    def check():
        if time.monotonic() - started > 5:
            raise TimeoutError("probe deadline")

    with pytest.raises(OverflowError, match="document_tool_output_limit"):
        processes.CaptureDocumentTool(
            (sys.executable, "-c", "import os\nwhile True: os.write(1,b'x'*65536)"),
            maximum_bytes=1000,
            check=check,
        )
    assert time.monotonic() - started < 5


@pytest.mark.skipif(os.name != "posix", reason="POSIX descendant process probe")
def test_timeout_reaps_group_even_after_leader_exits(tmp_path):
    pid_file = tmp_path / "pid"
    child = "import os,signal,time,pathlib; pathlib.Path(__import__('sys').argv[1]).write_text(str(os.getpid())); signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    program = "import subprocess,sys; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]])"
    started = time.monotonic()

    def check():
        if time.monotonic() - started > 1:
            raise TimeoutError("probe deadline")

    with pytest.raises(TimeoutError):
        processes.CaptureDocumentTool(
            (sys.executable, "-c", program, child, str(pid_file)), maximum_bytes=1000, check=check
        )
    pid = int(pid_file.read_text())
    status = Path(f"/proc/{pid}/stat")
    until = time.monotonic() + 2
    while status.exists() and status.read_text().split()[2] != "Z" and time.monotonic() < until:
        time.sleep(0.01)
    assert not status.exists() or status.read_text().split()[2] == "Z"


@pytest.mark.parametrize("assign_ok", [True, False])
def test_windows_job_kills_descendants_on_close_and_rejects_unowned_parser(monkeypatch, assign_ok):
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
        OpenProcess=Api("open", 12), AssignProcessToJobObject=Api("assign", int(assign_ok)),
        CloseHandle=Api("close", 1),
        TerminateJobObject=Api("terminate", 1), QueryInformationJobObject=Api("query", 1),
    )
    monkeypatch.setattr(processes, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(processes.ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(processes.ctypes, "WinError", lambda code: OSError(code, "job failed"), raising=False)
    monkeypatch.setattr(processes.ctypes, "get_last_error", lambda: 5, raising=False)
    process = SimpleNamespace(pid=123, wait=lambda **kwargs: calls.append(("wait", ())))
    if not assign_ok:
        with pytest.raises(OSError):
            processes.DocumentProcessFamily(process)
    else:
        family = processes.DocumentProcessFamily(process)
        family.close()
        assert calls[-2:] == [("close", (11,)), ("wait", ())]
    assert ("close", (12,)) in calls and ("close", (11,)) in calls
