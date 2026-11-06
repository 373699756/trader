from __future__ import annotations

import pytest

from trader.infra.process_lock import ProcessLock, ProcessLockError


def test_process_lock_rejects_a_second_owner(tmp_path) -> None:
    first = ProcessLock(tmp_path / "server.lock")
    second = ProcessLock(tmp_path / "server.lock")

    first.acquire()
    try:
        with pytest.raises(ProcessLockError, match="already running"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()
