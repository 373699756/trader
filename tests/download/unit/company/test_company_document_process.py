"""Document capture uses the shared owner for bounded output and descendant cleanup."""

import os
import sys
import time
from pathlib import Path

import pytest

from trader.download.infra import company_document_process as processes


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
