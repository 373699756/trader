"""Driver-owned resource, size, cancellation and physical-attempt contracts."""

import gzip
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from trader.infra import http_transport as driver
from trader.infra.http_contracts import (
    HttpCancelled,
    HttpError,
    HttpRequest,
    HttpResponse,
    HttpResponseTooLarge,
    HttpStatusError,
    HttpTimeout,
)


def test_injected_attempt_inherits_absolute_deadline_and_reports_observation():
    requests = []
    observations = []
    inspected = []

    def execute(request):
        requests.append(request)
        return HttpResponse(200, b"ok")

    with driver.RequestsHttpTransport(
        monotonic=lambda: 20.0, execute_request=execute,
        observe=observations.append, inspect_response=lambda request, response: inspected.append(response),
    ) as transport:
        assert transport.get("https://example.invalid", timeout=5, deadline=22).content == b"ok"
    assert requests[0].deadline == 22
    assert len(observations) == len(inspected) == 1
    assert observations[0].status_code == 200
    assert observations[0].response_bytes == 2
    with pytest.raises(HttpError, match="closed"):
        transport.get("https://example.invalid", timeout=5)


@pytest.mark.parametrize("method", ["get", "post"])
@pytest.mark.parametrize("fail", [False, True])
def test_request_scope_releases_only_its_injected_transport_on_success_and_failure(method, fail):
    opened = []

    def execute(request):
        if fail:
            raise HttpTimeout("fixture_request_timeout")
        return HttpResponse(200, b"fixture")

    def open_transport():
        transport = driver.RequestsHttpTransport(execute_request=execute)
        opened.append(transport)
        return transport

    scope = driver.HttpRequestScope(open_transport)

    def request():
        if method == "get":
            return scope.get("https://example.invalid", timeout=2)
        return scope.post("https://example.invalid", headers={}, json={"fixture": True}, timeout=2)

    if fail:
        with pytest.raises(HttpTimeout):
            request()
    else:
        assert request().content == b"fixture"
    assert len(opened) == 1
    with pytest.raises(HttpError, match="closed"):
        opened[0].get("https://example.invalid", timeout=2)


class _Raw:
    status_code = 200
    encoding = "utf-8"
    connection = None

    def __init__(self, chunks, *, headers=None, on_read=lambda: None):
        self.raw = self
        self.headers = headers or {}
        self._chunks = iter(chunks)
        self.on_read = on_read
        self.closed = False

    def read1(self, _size, *, decode_content):
        assert decode_content is False
        self.on_read()
        return next(self._chunks, b"")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True


class _Session:
    trust_env = True

    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response

    def close(self):
        self.closed = True


def test_lazy_creation_reuse_close_and_no_environment_proxy_or_hidden_retry(monkeypatch):
    raw = _Raw([b"{}"])
    session = _Session(raw)
    created = []

    def create():
        created.append(session)
        return session

    monkeypatch.setattr(driver.requests, "Session", create)
    transport = driver.RequestsHttpTransport()
    assert not created
    with transport:
        assert transport.get("https://example.invalid/", timeout=1).json() == {}
        transport.get("https://example.invalid/", timeout=1)
    assert created == [session]
    assert session.trust_env is False and session.closed and raw.closed
    assert len(session.calls) == 2
    assert all(call[1]["allow_redirects"] is False for call in session.calls)
    with pytest.raises(HttpError, match="closed"):
        transport.get("https://example.invalid/", timeout=1)


@pytest.mark.parametrize("backend", ("injected", "session"))
def test_close_reports_pending_without_waiting_and_inflight_owner_closes_after_release(monkeypatch, backend):
    entered, release, waiter_started = threading.Event(), threading.Event(), threading.Event()
    injected, observations, closed = [], [], []

    def block():
        entered.set()
        assert release.wait(timeout=5)

    def execute(request):
        injected.append(request)
        block()
        assert request.cancel_requested()
        return HttpResponse(200, b"fixture")

    raw = _Raw([b"fixture"], on_read=block)
    session = _Session(raw)

    def close_session():
        closed.append(session)
        session.closed = True

    monkeypatch.setattr(session, "close", close_session)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    transport = driver.RequestsHttpTransport(
        execute_request=execute if backend == "injected" else None, observe=observations.append,
    )

    def waiting_cancelled():
        waiter_started.set()
        return False

    with ThreadPoolExecutor(max_workers=3) as executor:
        running = executor.submit(transport.get, "https://example.invalid/", timeout=120)
        try:
            assert entered.wait(timeout=1)
            waiting = executor.submit(
                transport.get, "https://example.invalid/waiter", timeout=120, cancel_requested=waiting_cancelled,
            )
            assert waiter_started.wait(timeout=1)
            closing = executor.submit(transport.close)
            with pytest.raises(HttpTimeout, match="http_transport_close_pending"):
                closing.result(timeout=1)
            assert not transport.is_closed() and not session.closed
            with pytest.raises(HttpCancelled, match="http_transport_closed"):
                waiting.result(timeout=1)
            with pytest.raises(HttpError, match="http_transport_closed"):
                transport.get("https://example.invalid/new", timeout=120)
        finally:
            release.set()
        with pytest.raises(HttpCancelled, match="http_transport_closed"):
            running.result(timeout=1)
    assert transport.is_closed()
    assert observations[0].failure == "HttpCancelled"
    assert len(injected) == (1 if backend == "injected" else 0)
    assert len(session.calls) == (1 if backend == "session" else 0)
    assert closed == ([session] if backend == "session" else [])
    if backend == "session":
        assert raw.closed
    transport.close()
    assert closed == ([session] if backend == "session" else [])


def test_session_close_failure_is_observable_and_can_be_retried_without_reopening_requests(monkeypatch):
    session = _Session(_Raw([b"fixture"]))
    closed = []

    def close_session():
        closed.append(session)
        if len(closed) == 1:
            raise OSError("controlled close failure")
        session.closed = True

    monkeypatch.setattr(session, "close", close_session)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    transport = driver.RequestsHttpTransport()
    transport.get("https://example.invalid/", timeout=1)
    with pytest.raises(HttpError, match="http_transport_close_failed") as caught:
        transport.close()
    assert isinstance(caught.value.__cause__, OSError)
    assert not transport.is_closed() and not session.closed
    with pytest.raises(HttpError, match="http_transport_closed"):
        transport.get("https://example.invalid/new", timeout=1)
    transport.close()
    assert transport.is_closed() and session.closed and len(closed) == 2


def test_streaming_response_is_delivered_to_sink_without_accumulated_body(monkeypatch):
    chunks = [b"a" * 65536, b"b" * 65536, b"tail"]
    raw = _Raw(chunks, headers={"Content-Length": str(sum(map(len, chunks)))})
    monkeypatch.setattr(driver.requests, "Session", lambda: _Session(raw))
    received, headers, attempts = [], [], []
    with driver.RequestsHttpTransport(observe=attempts.append) as transport:
        response = transport.execute(HttpRequest("GET", "https://example.invalid/pdf", 1,
            receive_headers=lambda status, fields: headers.append((status, fields)), receive_chunk=received.append))
    assert response.content == b"" and received == chunks
    assert headers[0][0] == 200 and raw.closed
    assert attempts[0].response_bytes == sum(map(len, chunks))


@pytest.mark.parametrize("compressed", [False, True])
def test_size_limit_counts_decompressed_bytes_and_closes_response(monkeypatch, compressed):
    raw = _Raw(
        [gzip.compress(b"x" * 1024) if compressed else b"x" * 1024],
        headers={"Content-Encoding": "gzip"} if compressed else {},
    )
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    attempts = []
    with driver.RequestsHttpTransport(observe=attempts.append) as transport, pytest.raises(HttpResponseTooLarge):
        transport.get("https://example.invalid/secret?token=hidden", timeout=1, max_response_bytes=128)
    assert raw.closed and len(session.calls) == 1
    assert attempts[0].failure == "HttpResponseTooLarge"
    assert "hidden" not in repr(attempts) and "secret" not in repr(attempts)


def test_gzip_and_case_insensitive_headers_are_project_values(monkeypatch):
    raw = _Raw([gzip.compress(b'{"ok": true}')], headers={"Content-Encoding": "gzip", "retry-after": "2"})
    monkeypatch.setattr(driver.requests, "Session", lambda: _Session(raw))
    with driver.RequestsHttpTransport() as transport:
        response = transport.get("https://example.invalid/", timeout=1)
    assert response.json() == {"ok": True}
    assert response.headers["Retry-After"] == "2"
    with pytest.raises(TypeError):
        response.headers["Retry-After"] = "3"


@pytest.mark.parametrize("declared,received", (((), "normal"), (("normal",), "unknown")))
def test_nonstandard_encoding_requires_an_exact_explicit_identity_declaration(monkeypatch, declared, received):
    raw = _Raw([b'{"ok": true}'], headers={"Content-Encoding": received})
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport(identity_content_encodings=declared) as transport, pytest.raises(
        HttpError, match="http_content_encoding_unsupported"
    ):
        transport.get("https://example.invalid/", timeout=1)
    assert raw.closed and session.closed and len(session.calls) == 1


def test_explicit_identity_encoding_preserves_the_original_response_header(monkeypatch):
    raw = _Raw([b'{"ok": true}'], headers={"Content-Encoding": "normal"})
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport(identity_content_encodings=("normal",)) as transport:
        response = transport.get("https://example.invalid/", timeout=1)
    assert response.json() == {"ok": True}
    assert response.headers["Content-Encoding"] == "normal"
    assert raw.closed and session.closed and len(session.calls) == 1


@pytest.mark.parametrize("encodings", (("",), ("Normal",), ("gzip",), ("deflate",), ("bad,normal",), (" leading",), ("bad/path",), ["normal"]))
def test_invalid_identity_encoding_configuration_is_rejected_without_io(monkeypatch, encodings):
    monkeypatch.setattr(driver.requests, "Session", lambda: pytest.fail("invalid configuration must not create I/O"))
    with pytest.raises(ValueError, match="http_identity_content_encodings_invalid"):
        driver.RequestsHttpTransport(identity_content_encodings=encodings)


def test_explicit_identity_encoding_does_not_bypass_the_response_limit(monkeypatch):
    raw = _Raw([b"x" * 129], headers={"Content-Encoding": "normal"})
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport(identity_content_encodings=("normal",)) as transport, pytest.raises(HttpResponseTooLarge):
        transport.get("https://example.invalid/", timeout=1, max_response_bytes=128)
    assert raw.closed and session.closed and len(session.calls) == 1


def test_explicit_identity_encoding_still_cancels_during_a_stream(monkeypatch):
    cancelled = threading.Event()
    raw = _Raw([b"x", b"y"], headers={"Content-Encoding": "normal"}, on_read=cancelled.set)
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport(identity_content_encodings=("normal",)) as transport, pytest.raises(HttpCancelled):
        transport.get("https://example.invalid/", timeout=1, cancel_requested=cancelled.is_set)
    assert raw.closed and session.closed and len(session.calls) == 1


def test_explicit_identity_encoding_keeps_the_total_deadline(monkeypatch):
    clock = [0.0]

    def advance():
        clock[0] += 2

    raw = _Raw([b"x", b"y"], headers={"Content-Encoding": "normal"}, on_read=advance)
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport(identity_content_encodings=("normal",), monotonic=lambda: clock[0]) as transport, pytest.raises(HttpTimeout):
        transport.get("https://example.invalid/", timeout=1)
    assert raw.closed and session.closed and len(session.calls) == 1


def test_json_honors_the_supplier_response_charset():
    response = HttpResponse(200, '{"name": "股票"}'.encode("gb18030"), encoding="gb18030")
    assert response.json() == {"name": "股票"}


def test_cancel_during_stream_closes_it_without_retry(monkeypatch):
    cancelled = threading.Event()
    raw = _Raw([b"x", b"y"], on_read=cancelled.set)
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport() as transport, pytest.raises(HttpCancelled):
        transport.get("https://example.invalid/", timeout=1, cancel_requested=cancelled.is_set)
    assert raw.closed and len(session.calls) == 1


@pytest.mark.parametrize("status", [302, 429, 500])
def test_status_errors_do_not_expose_payload(status):
    with pytest.raises(HttpStatusError) as caught:
        HttpResponse(status, b"private upstream body").raise_for_status()
    assert caught.value.status_code == status
    assert str(caught.value) == f"http_{status}"


def test_invalid_or_cancelled_request_performs_no_io(monkeypatch):
    monkeypatch.setattr(driver.requests, "Session", lambda: pytest.fail("unexpected I/O"))
    with pytest.raises(ValueError):
        HttpRequest("GET", "https://example.invalid", float("nan"))
    with driver.RequestsHttpTransport() as transport, pytest.raises(HttpCancelled):
        transport.get("https://example.invalid", timeout=1, cancel_requested=lambda: True)


@pytest.mark.parametrize("value", (0, -1, float("nan"), float("inf")))
def test_invalid_io_timeout_is_rejected(value):
    with pytest.raises(ValueError, match="http_io_timeout_invalid"):
        HttpRequest("GET", "https://example.invalid", 120, io_timeout_seconds=value)


def test_progressing_download_can_exceed_io_timeout_but_not_total_deadline(monkeypatch):
    clock = [0.0]
    socket_timeouts = []

    class Socket:
        def settimeout(self, seconds):
            socket_timeouts.append(seconds)

    class Connection:
        sock = Socket()

    def advance():
        clock[0] += 3

    raw = _Raw([b"%PDF-", b"content", b"end"], on_read=advance)
    raw.connection = Connection()
    session = _Session(raw)
    monkeypatch.setattr(driver.requests, "Session", lambda: session)
    with driver.RequestsHttpTransport(monotonic=lambda: clock[0]) as transport:
        response = transport.execute(HttpRequest("GET", "https://example.invalid", 120, io_timeout_seconds=8))
    assert response.content == b"%PDF-contentend" and clock[0] > 8
    assert session.calls[0][1]["timeout"] == 8 and max(socket_timeouts) == 8
    assert raw.closed
    clock[0] = 0.0
    raw = _Raw([b"a"] * 20, on_read=advance)
    raw.connection = Connection()
    monkeypatch.setattr(driver.requests, "Session", lambda: _Session(raw))
    with driver.RequestsHttpTransport(monotonic=lambda: clock[0]) as transport, pytest.raises(HttpTimeout):
        transport.execute(HttpRequest("GET", "https://example.invalid", 10, io_timeout_seconds=8))
    assert clock[0] <= 13 and raw.closed


def test_total_deadline_interrupts_a_continuously_trickling_body():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.01)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with driver.RequestsHttpTransport() as transport:
            started = time.monotonic()
            with pytest.raises(HttpTimeout):
                transport.get(f"http://127.0.0.1:{server.server_port}/", timeout=0.15)
            assert time.monotonic() - started < 0.6
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)
