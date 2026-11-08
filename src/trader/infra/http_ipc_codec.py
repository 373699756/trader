"""Explicit HTTP wire fields; worker results never unpickle code in the parent."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import cast

from trader.infra.http_contracts import HttpRequest, HttpResponse
from trader.infra.ipc_frames import ReceiveIpcFrame, SendIpcFrame

_MAXIMUM_METADATA_BYTES = 64 * 1024
_MAXIMUM_REQUEST_BYTES = 16 * 1024 * 1024


def SendHttpRequest(connection: socket.socket, request: HttpRequest, check: Callable[[], float]) -> None:
    if request.receive_headers is not None or request.receive_chunk is not None:
        raise ValueError("isolated_http_stream_callbacks_unsupported")
    if request.body is not None and len(request.body) > _MAXIMUM_REQUEST_BYTES:
        raise ValueError("isolated_http_request_too_large")
    metadata = {
        "method": request.method, "url": request.url, "timeout_seconds": request.timeout_seconds,
        "headers": request.headers, "has_body": request.body is not None, "deadline": request.deadline,
        "max_response_bytes": request.max_response_bytes, "io_timeout_seconds": request.io_timeout_seconds,
    }
    _send_metadata(connection, metadata, check)
    SendIpcFrame(connection, request.body or b"", check=check)


def ReceiveHttpRequest(connection: socket.socket) -> HttpRequest:
    metadata = _receive_metadata(connection)
    method, url = metadata["method"], metadata["url"]
    if method not in {"GET", "POST", "DELETE"} or not isinstance(url, str):
        raise ValueError("isolated_http_request_invalid")
    timeout, deadline = metadata["timeout_seconds"], metadata["deadline"]
    io_timeout, limit = metadata["io_timeout_seconds"], metadata["max_response_bytes"]
    has_body = metadata["has_body"]
    if (
        type(timeout) not in {int, float} or (deadline is not None and type(deadline) not in {int, float})
        or (io_timeout is not None and type(io_timeout) not in {int, float})
        or type(limit) is not int or type(has_body) is not bool
    ):
        raise ValueError("isolated_http_request_invalid")
    body = ReceiveIpcFrame(connection, maximum_bytes=_MAXIMUM_REQUEST_BYTES)
    return HttpRequest(
        method,
        url,
        cast("float", timeout),
        headers=_headers(metadata["headers"]),
        body=body if has_body else None,
        deadline=cast("float | None", deadline),
        max_response_bytes=limit,
        io_timeout_seconds=cast("float | None", io_timeout),
    )


def SendHttpOutcome(connection: socket.socket, response: HttpResponse | None, failure: str) -> None:
    metadata: dict[str, object] = {"failure": failure}
    if response is not None:
        metadata.update(status_code=response.status_code, headers=response.header_items, encoding=response.encoding)
    _send_metadata(connection, metadata)
    SendIpcFrame(connection, response.content if response is not None else b"")


def ReceiveHttpOutcome(
    connection: socket.socket, maximum_bytes: int, check: Callable[[], float],
) -> tuple[HttpResponse | None, str]:
    metadata = _receive_metadata(connection, check)
    content = ReceiveIpcFrame(connection, maximum_bytes=maximum_bytes, check=check)
    failure = metadata["failure"]
    if not isinstance(failure, str):
        raise ValueError("isolated_http_response_invalid")
    if failure:
        return None, failure
    status, encoding = metadata["status_code"], metadata["encoding"]
    if type(status) is not int or not isinstance(encoding, str):
        raise ValueError("isolated_http_response_invalid")
    return HttpResponse(status, content, _headers(metadata["headers"]), encoding), ""


def _send_metadata(
    connection: socket.socket, metadata: dict[str, object], check: Callable[[], float] | None = None,
) -> None:
    payload = json.dumps(metadata, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode("ascii")
    if len(payload) > _MAXIMUM_METADATA_BYTES:
        raise ValueError("isolated_http_metadata_too_large")
    SendIpcFrame(connection, payload, check=check)


def _receive_metadata(connection: socket.socket, check: Callable[[], float] | None = None) -> dict[str, object]:
    payload = ReceiveIpcFrame(connection, maximum_bytes=_MAXIMUM_METADATA_BYTES, check=check)
    decoded = json.loads(payload)
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        raise ValueError("isolated_http_metadata_invalid")
    if check is not None:
        check()
    return cast("dict[str, object]", decoded)


def _headers(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list):
        raise ValueError("isolated_http_headers_invalid")
    headers = []
    for item in value:
        if not isinstance(item, list) or len(item) != 2 or not all(isinstance(part, str) for part in item):
            raise ValueError("isolated_http_headers_invalid")
        headers.append((item[0], item[1]))
    return tuple(headers)


__all__ = [
    "ReceiveHttpOutcome",
    "ReceiveHttpRequest",
    "SendHttpOutcome",
    "SendHttpRequest",
]
