"""Single Requests driver; bounded responses, explicit ownership and zero retries."""

from __future__ import annotations

import json as json_codec
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
)


class RequestsHttpTransport:
    """One explicitly owned handle; concurrent borrowers never share a Session."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        observe: Callable[[HttpAttempt], None] | None = None,
        inspect_response: Callable[[HttpRequest, HttpResponse], None] | None = None,
        execute_request: Callable[[HttpRequest], HttpResponse] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._observe = observe
        self._inspect_response = inspect_response
        self._execute_request = execute_request
        self._lock = threading.Lock()
        self._session: requests.Session | None = None
        self._closed = False

    def __enter__(self) -> RequestsHttpTransport:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._session is not None:
                self._session.close()
                self._session = None

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
        if not self._lock.acquire(timeout=max(0.0, deadline - self._monotonic())):
            raise HttpTimeout("http_connection_wait_timeout")
        try:
            self._check(request, deadline)
            if self._closed:
                raise HttpError("http_transport_closed")
            if self._execute_request is not None:
                return self._injected_attempt(request, started, deadline)
            if self._session is None:
                self._session = requests.Session()
                self._session.trust_env = False
            return self._attempt(request, started, deadline)
        finally:
            self._lock.release()

    def _injected_attempt(self, request: HttpRequest, started: float, deadline: float) -> HttpResponse:
        assert self._execute_request is not None
        response: HttpResponse | None = None
        failure = ""
        try:
            response = self._execute_request(replace(request, deadline=deadline))
            self._check(request, deadline)
            if self._inspect_response is not None:
                self._inspect_response(request, response)
            return response
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
        failure = ""
        try:
            headers = {"Accept-Encoding": "gzip, deflate", **dict(request.headers)}
            with self._session.request(
                request.method,
                request.url,
                headers=headers,
                data=request.body,
                timeout=max(0.001, deadline - self._monotonic()),
                stream=True,
                allow_redirects=False,
            ) as raw:
                status = raw.status_code
                encoding = raw.headers.get("Content-Encoding", "identity").lower()
                if encoding not in {"identity", "gzip", "deflate"}:
                    raise HttpError("http_content_encoding_unsupported")
                decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding != "identity" else None
                wire_bytes = 0
                while True:
                    self._check(request, deadline)
                    connection = raw.raw.connection
                    if connection is not None and connection.sock is not None:
                        connection.sock.settimeout(max(0.001, deadline - self._monotonic()))
                    # read1 performs one socket read, including for trickling bodies.
                    chunk = raw.raw.read1(65536, decode_content=False)
                    if not chunk:
                        break
                    wire_bytes += len(chunk)
                    if wire_bytes > request.max_response_bytes:
                        raise HttpResponseTooLarge("http_response_too_large")
                    if decoder is not None:
                        chunk = decoder.decompress(chunk, request.max_response_bytes - len(body) + 1)
                    if len(body) + len(chunk) > request.max_response_bytes:
                        raise HttpResponseTooLarge("http_response_too_large")
                    body.extend(chunk)
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
                        len(body),
                        status,
                        failure or (f"http_{status}" if status is not None and status >= 300 else ""),
                    )
                )

    def _check(self, request: HttpRequest, deadline: float) -> None:
        if request.cancel_requested is not None and request.cancel_requested():
            raise HttpCancelled("http_cancelled")
        if self._monotonic() >= deadline:
            raise HttpTimeout("http_deadline_exceeded")


__all__ = ["RequestsHttpTransport"]
