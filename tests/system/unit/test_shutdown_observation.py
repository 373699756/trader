"""Shared task identity and server log projection stay independent of process control."""

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from trader.entrypoints import server
from trader.infra.linux_process import ParseLinuxTaskStat, ReadLinuxProcessIdentity
from trader.infra.shutdown import RecordShutdownEvent, ShutdownDeadline, ShutdownReport, ShutdownStep


def _stat(pid: int = 7412, *, birth: int = 1234) -> bytes:
    fields = [b"0"] * 20
    fields[0] = b"S"
    fields[7], fields[9], fields[11], fields[12], fields[19] = (b"101", b"7", b"70", b"30", str(birth).encode())
    return str(pid).encode() + b" (worker ) (with spaces)) " + b" ".join(fields)


def test_shared_stat_parser_preserves_parentheses_and_counter_coordinates():
    value = ParseLinuxTaskStat(_stat())
    assert (value.native_id, value.birth_ticks, value.comm) == (7412, 1234, "worker ) (with spaces)")
    assert (value.minor_page_faults, value.major_page_faults, value.user_cpu_ticks, value.system_cpu_ticks) == (101, 7, 70, 30)
    with pytest.raises(ValueError, match="linux_process_stat_invalid"):
        ParseLinuxTaskStat(b"malformed")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux proc identity required")
def test_process_identity_binds_boot_and_birth_without_silencing_broken_inputs(tmp_path: Path):
    root = tmp_path / "7412"
    root.mkdir()
    (root / "stat").write_bytes(_stat())
    boot = tmp_path / "sys/kernel/random/boot_id"
    boot.parent.mkdir(parents=True)
    boot.write_text("d50dbb32-ac67-4d74-99a9-7ad7db37c791\n", encoding="ascii")
    value = ReadLinuxProcessIdentity(7412, proc_root=tmp_path)
    assert value is not None
    assert (value.pid, value.birth_ticks, value.boot_id) == (7412, 1234, boot.read_text().strip())
    boot.unlink()
    with pytest.raises(FileNotFoundError):
        ReadLinuxProcessIdentity(7412, proc_root=tmp_path)
    (root / "stat").unlink()
    assert ReadLinuxProcessIdentity(7412, proc_root=tmp_path) is None


@pytest.mark.skipif(sys.platform != "linux", reason="Linux proc identity required")
def test_server_completed_projection_binds_the_original_deadline_and_process(monkeypatch):
    emitted, finished = [], threading.Event()

    def record(_message, raw):
        emitted.append(json.loads(raw))
        finished.set()

    monkeypatch.setattr(server._LOGGER, "warning", record)
    deadline = ShutdownDeadline.start(30)
    RecordShutdownEvent(deadline, "signal_received")
    RecordShutdownEvent(deadline, "return_ready")
    server._queue_shutdown_report(
        ShutdownReport.from_steps(deadline, (ShutdownStep("fixture", True, False),)), deadline,
    )
    assert finished.wait(3)
    identity = ReadLinuxProcessIdentity(os.getpid())
    assert identity is not None
    assert emitted[0]["process_identity"] == {
        "pid": identity.pid, "birth_ticks": identity.birth_ticks, "boot_id": identity.boot_id,
    }
    assert emitted[0]["started_at_monotonic"] == deadline.started_at_monotonic
    assert emitted[0]["report"]["completed"] is True
    assert [span["name"] for span in emitted[0]["timeline"]] == ["signal_received", "return_ready"]
