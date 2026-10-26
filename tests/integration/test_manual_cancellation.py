"""Exercise terminal Ctrl+C through run.sh against disposable local work only."""

import os
import select
import shlex
import signal
import sys
import time
from pathlib import Path
from threading import Event

import pytest

from trader.download.infra.company_evidence_index import SQLiteCompanyEvidenceIndex
from trader.infra.process_lock import ProcessLock
from trader.infra.signal_cancellation import ManualCancellation
from trader.infra.sqlite_connection import SqliteSession, SqliteTransaction

_ROOT = Path(__file__).resolve().parents[2]


def _read_until(descriptor, marker):
    output = bytearray()
    deadline = time.monotonic() + 15
    while marker not in output and time.monotonic() < deadline:
        if select.select([descriptor], [], [], 0.1)[0]:
            try:
                chunk = os.read(descriptor, 65536)
            except OSError:
                break
            if not chunk:
                break
            output.extend(chunk)
    assert marker in output, output.decode("utf-8", errors="replace")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux foreground terminal Ctrl+C contract")
@pytest.mark.parametrize("scenario", ("cooperative", "sql", "second", "deadline"))
def test_terminal_ctrl_c_exits_and_releases_lock_and_database(tmp_path, scenario):
    import pty

    bin_path = tmp_path / "venv" / "bin"
    bin_path.mkdir(parents=True)
    (bin_path / "python3").symlink_to(sys.executable)
    entrypoint = bin_path / "trader-cli"
    command = shlex.join([sys.executable, str(Path(__file__).resolve()), scenario, str(tmp_path)])
    entrypoint.write_text('#!/bin/sh\nif [ "$1" = "--help" ]; then exit 0; fi\nexec ' + command + ' "$@"\n')
    entrypoint.chmod(0o700)
    pid, descriptor = pty.fork()
    if pid == 0:
        os.chdir(_ROOT)
        environment = dict(os.environ, VENV_DIR=str(bin_path.parent), FORCE_INSTALL_DEPS="0")
        os.execvpe("bash", ["bash", str(_ROOT / "run.sh"), "sync", "company"], environment)
    reaped = False
    try:
        _read_until(descriptor, b"READY")
        started = time.monotonic()
        os.write(descriptor, b"\x03")
        _read_until(descriptor, "正在取消".encode())
        if scenario == "second":
            os.write(descriptor, b"\x03")
        while time.monotonic() - started < 10:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                reaped = True
                assert os.waitstatus_to_exitcode(status) == 130
                break
            time.sleep(0.02)
        assert reaped, "manual cancellation did not terminate its isolated process"
        with ProcessLock(tmp_path / "evidence.lock"):
            with SqliteSession(tmp_path / "evidence.sqlite3") as connection:
                assert tuple(connection.run("SELECT next_page,received_rows FROM partitions").require_one()) == (2, 1)
    finally:
        if not reaped:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
        os.close(descriptor)


def _child_main():
    scenario, directory = sys.argv[1:3]
    root = Path(directory)
    with ManualCancellation(timeout_seconds=0.8 if scenario == "deadline" else 30) as cancelled:
        with ProcessLock(root / "evidence.lock"):
            index = SQLiteCompanyEvidenceIndex(root / "evidence.sqlite3", cancel_requested=cancelled.is_set)
            index.initialize()
            with index._connect() as connection, SqliteTransaction(connection):
                connection.run(
                    "INSERT INTO partitions(market,start,end,next_page,received_rows) "
                    "VALUES('sse','2026-10-26','2026-10-26',2,1)"
                )
            if scenario == "sql":
                try:
                    with index._connect() as connection, SqliteTransaction(connection):
                        connection.run("UPDATE partitions SET next_page=999")
                        print("READY", flush=True)
                        connection.run(
                            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<1000000000) "
                            "SELECT sum(x) FROM n"
                        ).fetchall()
                except InterruptedError:
                    assert cancelled.is_set()
                else:
                    raise AssertionError("SQL unexpectedly completed without cancellation")
            else:
                print("READY", flush=True)
                try:
                    cancelled.wait(20)
                finally:
                    if scenario in {"second", "deadline"}:
                        Event().wait(20)  # Simulate a resource stuck during cleanup.
    return 130


if __name__ == "__main__":
    raise SystemExit(_child_main())
