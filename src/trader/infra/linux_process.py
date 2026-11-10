"""Linux task-stat decoding and process identities; no process control."""

from __future__ import annotations

import sys
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


@dataclass(frozen=True)
class LinuxTaskStat:
    native_id: int
    birth_ticks: int
    state: str
    comm: str
    user_cpu_ticks: int
    system_cpu_ticks: int
    minor_page_faults: int
    major_page_faults: int
    process_group_id: int


@dataclass(frozen=True)
class LinuxProcessIdentity:
    pid: int
    birth_ticks: int
    boot_id: str


def ParseLinuxTaskStat(contents: bytes) -> LinuxTaskStat:
    """Decode proc stat, including comm values containing spaces or parentheses."""
    try:
        prefix, separator, tail = contents.rpartition(b")")
        pid_text, opening, comm = prefix.partition(b"(")
        fields = tail.split()
        if not separator or not opening or len(fields) < 20 or len(fields[0]) != 1:
            raise ValueError("stat_shape_invalid")
        values = tuple(int(value) for value in (pid_text, fields[7], fields[9], fields[11], fields[12], fields[19], fields[2]))
        if values[0] <= 0 or any(value < 0 for value in values[1:]):
            raise ValueError("stat_counter_invalid")
        state = fields[0].decode("ascii")
    except (IndexError, UnicodeError, ValueError) as exc:
        raise ValueError("linux_process_stat_invalid") from exc
    return LinuxTaskStat(
        native_id=values[0], birth_ticks=values[5], state=state,
        comm=comm.decode("utf-8", errors="replace"),
        user_cpu_ticks=values[3], system_cpu_ticks=values[4],
        minor_page_faults=values[1], major_page_faults=values[2],
        process_group_id=values[6],
    )


def ReadLinuxProcessGroupMembers(
    group_id: int, *, proc_root: Path = Path("/proc"), timeout_seconds: float = 2.0,
) -> tuple[LinuxTaskStat, ...]:
    """Read live members before an owned, unreaped group leader releases its identity."""
    if type(group_id) is not int or group_id <= 0 or not 0 < timeout_seconds <= 5:
        raise ValueError("linux_process_group_options_invalid")
    deadline = time.monotonic() + timeout_seconds
    members: list[LinuxTaskStat] = []
    with os.scandir(proc_root) as entries:
        for count, entry in enumerate(entries):
            if count >= 32768 or time.monotonic() >= deadline:
                raise TimeoutError("linux_process_group_observation_incomplete")
            if not entry.name.isascii() or not entry.name.isdecimal():
                continue
            try:
                member = ParseLinuxTaskStat(_read_bounded(Path(entry.path) / "stat", 65536))
            except (FileNotFoundError, ProcessLookupError):
                continue
            if member.process_group_id == group_id and member.state not in {"Z", "X"}:
                members.append(member)
    return tuple(sorted(members, key=lambda member: member.native_id))


def ReadLinuxProcessIdentity(pid: int, *, proc_root: Path = Path("/proc")) -> LinuxProcessIdentity | None:
    """Bind one live task to this boot; disappearance or PID reuse returns None."""
    if sys.platform != "linux":
        raise RuntimeError("linux_process_identity_linux_required")
    if type(pid) is not int or pid <= 0:
        raise ValueError("linux_process_identity_pid_invalid")
    try:
        before = ParseLinuxTaskStat(_read_bounded(proc_root / str(pid) / "stat", 65536))
    except (FileNotFoundError, ProcessLookupError):
        return None
    boot = _read_bounded(proc_root / "sys/kernel/random/boot_id", 64).decode("ascii").strip()
    if str(UUID(boot)) != boot:
        raise ValueError("linux_process_identity_boot_invalid")
    try:
        after = ParseLinuxTaskStat(_read_bounded(proc_root / str(pid) / "stat", 65536))
    except (FileNotFoundError, ProcessLookupError):
        return None
    if before.native_id != pid or after.native_id != pid:
        raise ValueError("linux_process_identity_pid_mismatch")
    if before.birth_ticks != after.birth_ticks or after.state in {"Z", "X"}:
        return None
    return LinuxProcessIdentity(pid, after.birth_ticks, boot)


def _read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("linux_process_identity_file_too_large")
    return value


def OpenLinuxProcessDescriptor(identity: LinuxProcessIdentity) -> int:
    """Open a standard-library pidfd; unsupported runtimes are explicit."""
    try:
        operation = os.pidfd_open
    except AttributeError as exc:
        raise LinuxProcessCapabilityError("linux_pidfd_unavailable") from exc
    return operation(identity.pid, 0)


def SignalLinuxProcessDescriptor(descriptor: int, signum: int) -> None:
    """Signal the exact kernel task referenced by the caller's still-open pidfd."""
    try:
        operation = signal.pidfd_send_signal
    except AttributeError as exc:
        raise LinuxProcessCapabilityError("linux_pidfd_signal_unavailable") from exc
    operation(descriptor, signum, None, 0)


class LinuxProcessCapabilityError(RuntimeError):
    """This interpreter or platform does not expose the required kernel interface."""


__all__ = [
    "LinuxTaskStat", "LinuxProcessIdentity", "ParseLinuxTaskStat", "ReadLinuxProcessIdentity",
    "OpenLinuxProcessDescriptor", "SignalLinuxProcessDescriptor",
    "LinuxProcessCapabilityError",
    "ReadLinuxProcessGroupMembers",
]
