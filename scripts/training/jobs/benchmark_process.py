"""Cancel one owned Linux benchmark tree across process groups and sessions."""

from __future__ import annotations

import math
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

_PROC = Path("/proc")
_POLL_SECONDS = 0.05
_MAXIMUM_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True)
class _ProcessIdentity:
    pid: int
    birth_ticks: int


@dataclass(frozen=True)
class _Process:
    identity: _ProcessIdentity
    parent_pid: int
    state: str

    @property
    def running(self) -> bool:
        return self.state not in {"Z", "X", "x"}


def StopTrainingBenchmarkProcessTree(root_pid: int, *, timeout_seconds: float = 30.0) -> tuple[int, ...]:
    """Stop the root and its observed descendants, returning their admitted PIDs.

    Linux birth ticks are rechecked immediately before each individual signal;
    descendants remain owned when they start another session or are reparented.
    INT receives half the total budget, TERM another 30%, and KILL the remainder.
    Exited zombies require their parent's reap and are already stopped. This
    function does not reap another caller's children or signal process groups.
    """
    started = time.monotonic()
    if type(root_pid) is not int or root_pid <= 1 or root_pid == os.getpid():
        raise ValueError("training_benchmark_process_root_invalid")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool):
        raise ValueError("training_benchmark_process_timeout_invalid")
    try:
        budget = float(timeout_seconds)
    except OverflowError as exc:
        raise ValueError("training_benchmark_process_timeout_invalid") from exc
    if not math.isfinite(budget) or not 0 < budget <= _MAXIMUM_TIMEOUT_SECONDS:
        raise ValueError("training_benchmark_process_timeout_invalid")
    if os.name != "posix" or not (_PROC / "self/stat").is_file():
        raise RuntimeError("training_benchmark_process_linux_required")
    snapshot = _snapshot()
    protected = _ancestors(snapshot, os.getpid())
    if root_pid in protected:
        raise ValueError("training_benchmark_process_root_is_ancestor")
    root = snapshot.get(root_pid)
    if root is None or not root.running:
        return ()
    owned: dict[int, _ProcessIdentity] = {}
    for process in _descendants(snapshot, {root_pid}):
        _admit(process, owned, protected)
    if not owned:
        return ()
    for signum, fraction in ((signal.SIGINT, 0.5), (signal.SIGTERM, 0.8), (signal.SIGKILL, 1.0)):
        phase_deadline = started + budget * fraction
        _signal_owned(owned, signum)
        while True:
            snapshot = _snapshot()
            anchors = {
                pid for pid, identity in owned.items()
                if pid in snapshot and snapshot[pid].identity == identity and snapshot[pid].running
            }
            for process in _descendants(snapshot, anchors):
                if process.identity.pid not in owned and _admit(process, owned, protected):
                    _signal_owned({process.identity.pid: owned[process.identity.pid]}, signum)
            remaining = _running(owned, snapshot)
            if not remaining:
                return tuple(sorted(owned))
            wait_seconds = phase_deadline - time.monotonic()
            if wait_seconds <= 0:
                break
            time.sleep(min(_POLL_SECONDS, wait_seconds))
    remaining = _running(owned, _snapshot())
    if remaining:
        raise RuntimeError("training_benchmark_process_cleanup_incomplete:" + ",".join(map(str, remaining)))
    return tuple(sorted(owned))


def _read_process(pid: int) -> _Process | None:
    try:
        contents = (_PROC / str(pid) / "stat").read_bytes()
    except (FileNotFoundError, ProcessLookupError):
        return None
    # comm may contain whitespace and ')'; the final ')' terminates that field.
    tail = contents.rsplit(b")", 1)[1].split()
    return _Process(_ProcessIdentity(pid, int(tail[19])), int(tail[1]), tail[0].decode("ascii"))


def _snapshot() -> dict[int, _Process]:
    processes = {}
    for path in _PROC.iterdir():
        if path.name.isdecimal():
            process = _read_process(int(path.name))
            if process is not None:
                processes[process.identity.pid] = process
    return processes


def _ancestors(snapshot: dict[int, _Process], pid: int) -> set[int]:
    ancestors = set()
    while pid > 0 and pid not in ancestors:
        ancestors.add(pid)
        process = snapshot.get(pid)
        if process is None:
            break
        pid = process.parent_pid
    return ancestors


def _descendants(snapshot: dict[int, _Process], anchors: set[int]) -> tuple[_Process, ...]:
    included = set(anchors)
    while True:
        children = {pid for pid, process in snapshot.items() if process.parent_pid in included} - included
        if not children:
            return tuple(snapshot[pid] for pid in sorted(included) if pid in snapshot and snapshot[pid].running)
        included.update(children)


def _admit(process: _Process, owned: dict[int, _ProcessIdentity], protected: set[int]) -> bool:
    pid = process.identity.pid
    if pid in protected or pid <= 1:
        raise RuntimeError("training_benchmark_process_protected_descendant")
    if pid in owned:
        return False
    current = _read_process(pid)
    if current is None or current.identity != process.identity or not current.running:
        return False
    owned[pid] = process.identity
    return True


def _signal_owned(owned: dict[int, _ProcessIdentity], signum: signal.Signals) -> None:
    for identity in owned.values():
        current = _read_process(identity.pid)
        if current is not None and current.identity == identity and current.running:
            try:
                os.kill(identity.pid, signum)
            except ProcessLookupError:
                pass


def _running(owned: dict[int, _ProcessIdentity], snapshot: dict[int, _Process]) -> tuple[int, ...]:
    return tuple(sorted(
        pid for pid, identity in owned.items()
        if pid in snapshot and snapshot[pid].identity == identity and snapshot[pid].running
    ))


__all__ = ["StopTrainingBenchmarkProcessTree"]
