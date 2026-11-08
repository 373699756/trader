"""Bounded socket frames with cancellation checks throughout each transfer."""

from __future__ import annotations

import socket
import struct
from collections.abc import Callable

_HEADER = struct.Struct("!Q")
_CHUNK_BYTES = 64 * 1024


def SendIpcFrame(connection: socket.socket, payload: bytes, *, check: Callable[[], float] | None = None) -> None:
    """Send one frame; a supervised caller checks before every bounded write."""
    if check is None:
        connection.settimeout(None)
    for part in (memoryview(_HEADER.pack(len(payload))), memoryview(payload)):
        offset = 0
        while offset < len(part):
            if check is not None:
                connection.settimeout(min(0.05, max(0.000001, check())))
            try:
                written = connection.send(part[offset:offset + _CHUNK_BYTES])
            except TimeoutError:
                continue
            if written == 0:
                raise EOFError("ipc_peer_closed")
            offset += written
    if check is not None:
        check()


def ReceiveIpcFrame(
    connection: socket.socket, *, maximum_bytes: int, check: Callable[[], float] | None = None,
) -> bytes:
    """Reject oversized frames before allocating and supervise partial frames."""
    if check is None:
        connection.settimeout(None)
    header = _receive(connection, _HEADER.size, check)
    size, = _HEADER.unpack(header)
    if size > maximum_bytes:
        raise ValueError("ipc_frame_too_large")
    return _receive(connection, size, check)


def _receive(connection: socket.socket, size: int, check: Callable[[], float] | None) -> bytes:
    chunks = []
    received = 0
    while received < size:
        if check is not None:
            connection.settimeout(min(0.05, max(0.000001, check())))
        try:
            chunk = connection.recv(min(_CHUNK_BYTES, size - received))
        except TimeoutError:
            continue
        if not chunk:
            raise EOFError("ipc_peer_closed")
        chunks.append(chunk)
        received += len(chunk)
    if check is not None:
        check()
    return b"".join(chunks)


__all__ = [
    "ReceiveIpcFrame",
    "SendIpcFrame",
]
