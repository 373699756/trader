"""Driver-owned resource, size, cancellation and physical-attempt contracts."""

import gzip
import threading
import time
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
