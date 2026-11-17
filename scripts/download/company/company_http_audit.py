"""Exercise published company links through the real, locally bound HTTP application."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import ExitStack
from contextvars import ContextVar
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch
from urllib.parse import unquote, urlencode, urlsplit

from werkzeug.serving import WSGIRequestHandler, make_server

from trader import bootstrap
from trader.download.domain.company_document_links import CompanyDocumentLinkSnapshot
from trader.download.infra.company_facts_reader import SQLiteCompanyEvidenceReader
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.shutdown import ShutdownDeadline
from trader.infra.sqlite_connection import SqliteConnection

if TYPE_CHECKING:
    from _typeshed.wsgi import StartResponse, WSGIEnvironment

_CURRENT: ContextVar[_IoGuard | None] = ContextVar("company_http_audit_guard", default=None)
_HANDLER: ContextVar[bool] = ContextVar("company_http_audit_handler", default=False)
_HOOK_LOCK = threading.Lock()
_HOOK_INSTALLED = False


class _IoGuard:
    def __init__(self, roots: tuple[Path, ...], deadline: float) -> None:
        self.roots = roots
        self.deadline = deadline
        self.handler_file_attempts = 0
        self.handler_sql_attempts = 0
        self.handler_network_attempts = 0
        self.external_network_attempts = 0
        self.business_write_attempts = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("company_http_audit_deadline")

    def sql(self) -> None:
        if _HANDLER.get():
            self.handler_sql_attempts += 1
            raise PermissionError("company_http_handler_sql_forbidden")

    def file(self) -> None:
        if _HANDLER.get():
            self.handler_file_attempts += 1
            raise PermissionError("company_http_handler_file_forbidden")

    def protected(self, value: object) -> bool:
        if not isinstance(value, (str, bytes, os.PathLike)):
            return False
        # abspath is lexical: the audit hook must not recursively stat the path.
        path = os.fsdecode(value)
        if path.startswith("file:"):
            path = unquote(urlsplit(path).path)
        name = os.path.abspath(path)
        return any(name == str(root) or name.startswith(str(root) + os.sep) for root in self.roots)

    def audit(self, event: str, args: tuple[object, ...]) -> None:
        if event == "open":
            self.file()
            flags = args[2] if len(args) > 2 else 0
            if isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                if args and self.protected(args[0]):
                    self.business_write_attempts += 1
                    raise PermissionError("company_http_audit_business_write_forbidden")
        elif event in ("os.listdir", "os.scandir"):
            self.file()
        elif event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "os.link", "os.symlink", "os.truncate"):
            self.file()
            if any(self.protected(value) for value in args[:2]):
                self.business_write_attempts += 1
                raise PermissionError("company_http_audit_business_write_forbidden")
        elif event == "sqlite3.connect":
            self.sql()
            if args and isinstance(args[0], (str, Path)):
                database = str(args[0])
                if "mode=ro" not in database and self.protected(database):
                    self.business_write_attempts += 1
                    raise PermissionError("company_http_audit_sql_write_connection_forbidden")
        elif event in ("socket.connect", "socket.getaddrinfo"):
            host = args[1][0] if event == "socket.connect" and len(args) > 1 and isinstance(args[1], tuple) else args[0]
            if _HANDLER.get():
                self.handler_network_attempts += 1
                raise PermissionError("company_http_handler_network_forbidden")
            if host not in ("127.0.0.1", "::1", "localhost"):
                self.external_network_attempts += 1
                raise PermissionError("company_http_audit_external_network_forbidden")


def _audit_hook(event: str, args: tuple[object, ...]) -> None:
    guard = _CURRENT.get()
    if guard is not None:
        guard.audit(event, args)


def _install_hook() -> None:
    global _HOOK_INSTALLED
    with _HOOK_LOCK:
        if not _HOOK_INSTALLED:
            sys.addaudithook(_audit_hook)
            _HOOK_INSTALLED = True


class _QuietHandler(WSGIRequestHandler):
    def log(self, type: str, message: str, *args: object) -> None:
        """Do not disclose query strings or evidence identities in access logs."""


def _fingerprints(path: Path, check: Callable[[], None]) -> tuple[str | None, ...]:
    values: list[str | None] = []
    for suffix in ("", "-wal", ".sha256"):
        check()
        candidate = Path(str(path) + suffix)
        if not candidate.exists():
            values.append(None)
            continue
        digest = hashlib.sha256()
        with candidate.open("rb") as handle:
            while content := handle.read(1024 * 1024):
                check()
                digest.update(content)
        values.append(digest.hexdigest())
    return tuple(values)


def _expected(snapshot: CompanyDocumentLinkSnapshot, code: str, identities: tuple[str, ...]) -> list[dict[str, object]]:
    requested = frozenset(identities)
    selected = sorted((item.link for item in snapshot.links if item.code == code and item.link.evidence_id in requested),
                      key=lambda item: (item.published_at, item.evidence_id), reverse=True)
    return [{"evidence_id": item.evidence_id, "title": item.title,
             "url": item.url if item.url.startswith(("http://", "https://")) else None,
             "published_at": item.published_at.isoformat(), "body_checked": item.body_checked} for item in selected]


def _request(port: int, path: str, guard: _IoGuard) -> tuple[int, object]:
    guard.check()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=min(5.0, guard.deadline - time.monotonic()))
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        guard.check()
        if len(content) > 1024 * 1024:
            raise ValueError("company_http_response_too_large")
        return response.status, json.loads(content)
    finally:
        connection.close()


def AuditCompanyHttp(
    configuration: Path,
    *,
    expected_publication_hash: str,
    codes: tuple[str, ...],
    budget_seconds: float,
) -> dict[str, object]:
    """Read a real namespace and exercise only its published-document GET route.

    Guarded attempts fail closed, including attempts swallowed by the application.
    This is a functional acceptance check, without source or publication admission.
    """
    if (not math.isfinite(budget_seconds) or budget_seconds <= 0 or not codes or len(codes) > 12000
        or len(codes) != len(set(codes)) or any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in codes)
        or len(expected_publication_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_publication_hash)):
        raise ValueError("company_http_audit_arguments_invalid")
    started = time.monotonic()
    deadline = started + budget_seconds
    settings = LoadRuntimeSettings(configuration)
    guard = _IoGuard((settings.data_paths.root.resolve(), settings.runtime_dir.resolve()), deadline)
    _install_hook()
    before = _fingerprints(settings.data_paths.company, guard.check)
    token = _CURRENT.set(guard)
    requests = verified = invalid = 0
    failure: str | None = None
    loaded_identity = ""
    links_digest = hashlib.sha256()
    server_stopped = resources_stopped = False
    system = None
    server = None
    thread = None
    after: tuple[str | None, ...] | None = None
    try:
        guard.check()
        system = bootstrap.BuildSystem(configuration, owns_shared_runtime=False)
        guard.check()
        system.market_data.references.initialize()
        guard.check()
        system.market_data.research.initialize(codes, cancelled=lambda: time.monotonic() >= deadline)
        loaded_identity = system.market_data.research.loaded_identity
        if loaded_identity != expected_publication_hash:
            raise ValueError("company_http_loaded_identity_mismatch")
        reader = SQLiteCompanyEvidenceReader(settings.data_paths.company,
            event_policy=system.strategy.company_scoring.events, sqlite_policy=bootstrap.BuildSqlitePolicy(settings))
        snapshot = reader.read_document_links(codes, cancel_requested=lambda: time.monotonic() >= deadline)
        if snapshot.publication_hash != expected_publication_hash:
            raise ValueError("company_http_formal_identity_mismatch")
        application = system.app.wsgi_app

        def guarded_application(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
            current = _CURRENT.set(guard)
            handler = _HANDLER.set(True)
            try:
                guard.check()
                yield from application(environ, start_response)
            finally:
                _HANDLER.reset(handler)
                _CURRENT.reset(current)

        def checked_stat(call: Callable[..., object]) -> Callable[..., object]:
            def invoke(*args: object, **kwargs: object) -> object:
                current = _CURRENT.get()
                if current is not None:
                    current.file()
                return call(*args, **kwargs)
            return invoke

        def checked_sql(call: Callable[..., object]) -> Callable[..., object]:
            def invoke(*args: object, **kwargs: object) -> object:
                current = _CURRENT.get()
                if current is not None:
                    current.sql()
                return call(*args, **kwargs)
            return invoke

        with ExitStack() as checks:
            checks.enter_context(patch.object(system.app, "wsgi_app", guarded_application))
            for name in ("stat", "lstat", "access", "readlink"):
                checks.enter_context(patch.object(os, name, checked_stat(getattr(os, name))))
            for name in ("run", "run_many"):
                checks.enter_context(patch.object(SqliteConnection, name, checked_sql(getattr(SqliteConnection, name))))
            server = make_server("127.0.0.1", 0, system.app, request_handler=_QuietHandler)
            server.timeout = 0.05
            stop = threading.Event()

            def serve() -> None:
                while not stop.is_set():
                    server.handle_request()

            thread = threading.Thread(target=serve, name="company-http-audit", daemon=True)
            thread.start()
            try:
                for code in codes:
                    identities = tuple(item.link.evidence_id for item in snapshot.links if item.code == code)
                    if len(identities) != len(set(identities)):
                        raise ValueError("company_http_formal_links_ambiguous")
                    for offset in range(0, len(identities), 32):
                        group = identities[offset:offset + 32]
                        expected = {"schema_version": "company_documents", "code": code,
                                    "documents": _expected(snapshot, code, group)}
                        status, payload = _request(server.server_port,
                            f"/api/company/{code}/documents?{urlencode([('evidence_id', item) for item in group])}", guard)
                        requests += 1
                        if status != 200 or json.dumps(payload, sort_keys=True) != json.dumps(expected, sort_keys=True):
                            raise ValueError("company_http_published_link_mismatch")
                        links_digest.update(json.dumps(expected, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode())
                        verified += len(group)
                for path in ("/api/company/invalid/documents?evidence_id=x", f"/api/company/{codes[0]}/documents",
                             f"/api/company/{codes[0]}/documents?evidence_id=",
                             f"/api/company/{codes[0]}/documents?{urlencode([('evidence_id', str(i)) for i in range(33)])}",
                             f"/api/company/{codes[0]}/documents?evidence_id={'x' * 201}"):
                    status, _ = _request(server.server_port, path, guard)
                    requests += 1
                    if status != 400:
                        raise ValueError("company_http_invalid_query_not_rejected")
                    invalid += 1
            finally:
                stop.set()
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
                server.server_close()
                server_stopped = not thread.is_alive()
        if not verified:
            raise ValueError("company_http_no_published_links")
    except InterruptedError:
        raise
    except (OSError, RuntimeError, ValueError, http.client.HTTPException) as exc:
        failure = type(exc).__name__
    finally:
        try:
            if server is not None:
                server.server_close()
                server_stopped = thread is None or not thread.is_alive()
            if system is not None:
                try:
                    resources_stopped = system.stop(deadline=ShutdownDeadline.start(max(0.0, deadline - time.monotonic()))).completed
                except InterruptedError:
                    raise
                except (OSError, RuntimeError, ValueError) as exc:
                    failure = failure or type(exc).__name__
        finally:
            _CURRENT.reset(token)
        try:
            after = _fingerprints(settings.data_paths.company, guard.check)
        except TimeoutError:
            failure = failure or "TimeoutError"
    unchanged = after is not None and before == after
    exhausted = time.monotonic() >= deadline
    counts = (guard.handler_file_attempts, guard.handler_sql_attempts, guard.handler_network_attempts,
              guard.external_network_attempts, guard.business_write_attempts)
    passed = failure is None and not any(counts) and unchanged and server_stopped and resources_stopped and not exhausted
    return {"scope": "published_company_document_http", "passed": passed, "complete": passed,
            "expected_publication_hash": expected_publication_hash, "loaded_identity": loaded_identity,
            "codes": len(codes), "verified_links": verified, "http_requests": requests, "invalid_queries_rejected": invalid,
            "links_digest": links_digest.hexdigest(), "handler_file_attempts": guard.handler_file_attempts,
            "handler_sql_attempts": guard.handler_sql_attempts, "handler_network_attempts": guard.handler_network_attempts,
            "external_network_attempts": guard.external_network_attempts, "business_write_attempts": guard.business_write_attempts,
            "source_unchanged": unchanged, "source_payload_sha256_before": list(before), "source_payload_sha256_after": list(after) if after is not None else [],
            "server_stopped": server_stopped, "resources_stopped": resources_stopped, "budget_exhausted": exhausted,
            "failure": failure, "source_admission_verified": False, "production_enabled": False,
            "full_sync_verified": False, "performance_accepted": False}
