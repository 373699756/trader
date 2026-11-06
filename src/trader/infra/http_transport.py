"""Single Requests driver; bounded responses, explicit ownership and zero retries."""

from __future__ import annotations

import json as json_codec
import re
import threading
import time
import zlib
from collections.abc import Callable, Mapping
from dataclasses import replace
from urllib.parse import urlencode, urlsplit

import requests
from urllib3.exceptions import HTTPError as UrllibHttpError
from urllib3.exceptions import ReadTimeoutError

from trader.infra.http_contracts import (
    EncodeHttpParameters,
    HttpAttempt,
    HttpCancelled,
    HttpConnectionError,
    HttpError,
    HttpParameter,
    HttpRequest,
    HttpResponse,
    HttpResponseTooLarge,
    HttpTimeout,
    HttpTransport,
)


class HttpRequestScope:
    """Own one request's transport lifetime through an explicitly injected factory."""

    def __init__(self, open_transport: Callable[[], HttpTransport]) -> None:
        self._open_transport = open_transport

    def get(
        self,
        url: str,
        *,
        timeout: float,
        params: Mapping[str, HttpParameter] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        with self._open_transport() as transport:
            return transport.get(url, timeout=timeout, params=params, headers=headers, max_response_bytes=32 * 1024 * 1024)

    def post(self, url: str, *, headers: Mapping[str, str], json: object, timeout: float) -> HttpResponse:
        with self._open_transport() as transport:
            return transport.post(url, headers=headers, json=json, timeout=timeout)


class RequestsHttpTransport:
    """One explicitly owned handle; concurrent borrowers never share a Session."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        observe: Callable[[HttpAttempt], None] | None = None,
        inspect_response: Callable[[HttpRequest, HttpResponse], None] | None = None,
        execute_request: Callable[[HttpRequest], HttpResponse] | None = None,
        identity_content_encodings: tuple[str, ...] = (),
    ) -> None:
        if not isinstance(identity_content_encodings, tuple) or any(
            not isinstance(encoding, str)
            or re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", encoding) is None
            or encoding in {"gzip", "deflate"}
            for encoding in identity_content_encodings
        ):
            raise ValueError("http_identity_content_encodings_invalid")
        self._monotonic = monotonic
        self._observe = observe
        self._inspect_response = inspect_response
        self._execute_request = execute_request
        self._identity_content_encodings = frozenset(identity_content_encodings)
        self._lock = threading.Lock()
        self._session: requests.Session | None = None
        self._close_requested = threading.Event()
        self._closed = threading.Event()

    def __enter__(self) -> RequestsHttpTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._close_requested.set()
        if self._closed.is_set():
            return
        if not self._lock.acquire(blocking=False):
            raise HttpTimeout("http_transport_close_pending")
        try:
            self._close_session()
        finally:
            self._lock.release()

    def is_closed(self) -> bool:
        return self._closed.is_set()

    def _close_session(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except (OSError, requests.RequestException) as exc:
                raise HttpError("http_transport_close_failed") from exc
            self._session = None
        self._closed.set()

    def get(
        self,
        url: str,
        *,
        timeout: float,
        params: Mapping[str, HttpParameter] | None = None,
        headers: Mapping[str, str] | None = None,
        deadline: float | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HttpResponse:
        return self.execute(
            HttpRequest(
                "GET",
                EncodeHttpParameters(url, params),
                timeout,
                tuple((headers or {}).items()),
                deadline=deadline,
                max_response_bytes=max_response_bytes,
                cancel_requested=cancel_requested,
            )
        )

    def post(
        self,
        url: str,
        *,
        timeout: float,
        params: Mapping[str, HttpParameter] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        json: object = None,
        deadline: float | None = None,
        max_response_bytes: int = 16 * 1024 * 1024,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> HttpResponse:
        if data is not None and json is not None:
            raise ValueError("http_body_conflict")
        request_headers = dict(headers or {})
        body = None
        if data is not None:
            body = urlencode(data).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif json is not None:
            body = json_codec.dumps(json, allow_nan=False, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        return self.execute(
            HttpRequest(
                "POST",
                EncodeHttpParameters(url, params),
                timeout,
                tuple(request_headers.items()),
                body,
                deadline,
                max_response_bytes,
                cancel_requested,
            )
        )

    def execute(self, request: HttpRequest) -> HttpResponse:
        started = self._monotonic()
        deadline = (
            min(request.deadline, started + request.timeout_seconds)
            if request.deadline is not None
            else started + request.timeout_seconds
        )
        self._check(request, deadline)
        self._acquire_connection(request, deadline)
        try:
            self._check(request, deadline)
            if self._execute_request is not None:
                return self._injected_attempt(request, started, deadline)
            if self._session is None:
                self._session = requests.Session()
                self._session.trust_env = False
            return self._attempt(request, started, deadline)
        finally:
            try:
                if self._close_requested.is_set():
                    self._close_session()
            finally:
                self._lock.release()

    def _acquire_connection(self, request: HttpRequest, deadline: float) -> None:
        while True:
            self._check_cancelled(request)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise HttpTimeout("http_connection_wait_timeout")
            if self._lock.acquire(timeout=min(remaining, 0.05)):
                return

    def _injected_attempt(self, request: HttpRequest, started: float, deadline: float) -> HttpResponse:
        assert self._execute_request is not None
        response: HttpResponse | None = None
        failure = ""
        cancel_requested = request.cancel_requested

        def cancelled() -> bool:
            return self._close_requested.is_set() or (cancel_requested is not None and cancel_requested())

        try:
            response = self._execute_request(replace(request, deadline=deadline, cancel_requested=cancelled))
            self._check(request, deadline)
            if len(response.content) > request.max_response_bytes:
                raise HttpResponseTooLarge("http_response_too_large")
            if request.receive_headers is not None and request.receive_chunk is not None:
                request.receive_headers(response.status_code, response.header_items)
                for offset in range(0, len(response.content), 65536):
                    self._check(request, deadline)
                    request.receive_chunk(response.content[offset:offset + 65536])
            if self._inspect_response is not None:
                self._inspect_response(request, response)
            return replace(response, content=b"") if request.receive_chunk is not None else response
        except HttpError as exc:
            failure = type(exc).__name__
            raise
        finally:
            if self._observe is not None:
                self._observe(HttpAttempt(
                    urlsplit(request.url).hostname or "", request.method,
                    self._monotonic() - started,
                    len(response.content) if response is not None else 0,
                    response.status_code if response is not None else None,
                    failure or (f"http_{response.status_code}" if response is not None and response.status_code >= 300 else ""),
                ))

    def _attempt(self, request: HttpRequest, started: float, deadline: float) -> HttpResponse:
        assert self._session is not None
        status: int | None = None
        body = bytearray()
        received = 0
        failure = ""
        try:
            headers = {"Accept-Encoding": "gzip, deflate", **dict(request.headers)}
            with self._session.request(
                request.method,
                request.url,
                headers=headers,
                data=request.body,
                timeout=max(0.001, min(deadline - self._monotonic(), request.io_timeout_seconds or request.timeout_seconds)),
                stream=True,
                allow_redirects=False,
            ) as raw:
                status = raw.status_code
                if request.receive_headers is not None:
                    request.receive_headers(status, tuple(raw.headers.items()))
                encoding = raw.headers.get("Content-Encoding", "identity").lower()
                if encoding in self._identity_content_encodings:
                    encoding = "identity"
                if encoding not in {"identity", "gzip", "deflate"}:
                    raise HttpError("http_content_encoding_unsupported")
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding != "identity" else None
                wire_bytes = 0
                while True:
                    self._check(request, deadline)
                    connection = raw.raw.connection
                    if connection is not None and connection.sock is not None:
                        connection.sock.settimeout(max(0.001, min(
                            deadline - self._monotonic(), request.io_timeout_seconds or request.timeout_seconds
                        )))
                    # read1 performs one socket read, including for trickling bodies.
                    chunk = raw.raw.read1(65536, decode_content=False)
                    if not chunk:
                        break
                    wire_bytes += len(chunk)
                    if wire_bytes > request.max_response_bytes:
                        raise HttpResponseTooLarge("http_response_too_large")
                    if decoder is not None:
                        chunk = decoder.decompress(chunk, request.max_response_bytes - received + 1)
                    if received + len(chunk) > request.max_response_bytes:
                        raise HttpResponseTooLarge("http_response_too_large")
                    received += len(chunk)
                    if request.receive_chunk is None:
                        body.extend(chunk)
                    else:
                        request.receive_chunk(chunk)
                if decoder is not None and (not decoder.eof or decoder.unused_data):
                    raise HttpError("http_compressed_body_invalid")
                self._check(request, deadline)
                response = HttpResponse(status, bytes(body), tuple(raw.headers.items()), raw.encoding or "utf-8")
            if self._inspect_response is not None:
                self._inspect_response(request, response)
            return response
        except (requests.Timeout, ReadTimeoutError, TimeoutError) as exc:
            failure = "timeout"
            raise HttpTimeout("http_timeout") from exc
        except requests.ConnectionError as exc:
            failure = "connection_failed"
            raise HttpConnectionError("http_connection_failed") from exc
        except (requests.RequestException, UrllibHttpError, zlib.error) as exc:
            failure = "transport_failed"
            raise HttpError("http_transport_failed") from exc
        except HttpError as exc:
            failure = type(exc).__name__
            raise
        finally:
            if self._observe is not None:
                self._observe(
                    HttpAttempt(
                        urlsplit(request.url).hostname or "",
                        request.method,
                        self._monotonic() - started,
                        received,
                        status,
                        failure or (f"http_{status}" if status is not None and status >= 300 else ""),
                    )
                )

    def _check(self, request: HttpRequest, deadline: float) -> None:
        self._check_cancelled(request)
        if self._monotonic() >= deadline:
            raise HttpTimeout("http_deadline_exceeded")

    def _check_cancelled(self, request: HttpRequest) -> None:
        if self._close_requested.is_set():
            raise HttpCancelled("http_transport_closed")
        if request.cancel_requested is not None and request.cancel_requested():
            raise HttpCancelled("http_cancelled")


__all__ = [
    "HttpRequestScope",
    "RequestsHttpTransport",
]
