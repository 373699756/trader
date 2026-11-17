"""The HTTP acceptance guard fails closed without substituting production services."""

import sqlite3
from pathlib import Path

import pytest

from scripts.download.company import company_http_audit as audit


def _guard(tmp_path):
    return audit._IoGuard((tmp_path.resolve(),), float("inf"))


@pytest.mark.parametrize("event,args,counter", [
    ("open", ("example", "r", 0), "handler_file_attempts"),
    ("os.listdir", ("example",), "handler_file_attempts"),
    ("sqlite3.connect", ("example",), "handler_sql_attempts"),
    ("socket.connect", (None, ("127.0.0.1", 1)), "handler_network_attempts"),
])
def test_handler_io_is_denied_and_counted(tmp_path, event, args, counter):
    guard = _guard(tmp_path)
    token = audit._HANDLER.set(True)
    try:
        with pytest.raises(PermissionError):
            guard.audit(event, args)
    finally:
        audit._HANDLER.reset(token)
    assert getattr(guard, counter) == 1


def test_actual_python_file_open_is_blocked_by_the_scoped_audit_hook(tmp_path):
    path = tmp_path / "source"
    path.write_text("retained")
    guard = _guard(tmp_path)
    audit._install_hook()
    current = audit._CURRENT.set(guard)
    handler = audit._HANDLER.set(True)
    try:
        with pytest.raises(PermissionError, match="handler_file"):
            path.read_bytes()
    finally:
        audit._HANDLER.reset(handler)
        audit._CURRENT.reset(current)
    assert guard.handler_file_attempts == 1
    assert path.read_text() == "retained"


def test_actual_sqlite_connection_is_blocked_in_the_handler(tmp_path):
    guard = _guard(tmp_path)
    audit._install_hook()
    current = audit._CURRENT.set(guard)
    handler = audit._HANDLER.set(True)
    try:
        with pytest.raises(PermissionError, match="handler_sql"):
            sqlite3.connect(":memory:")
    finally:
        audit._HANDLER.reset(handler)
        audit._CURRENT.reset(current)
    assert guard.handler_sql_attempts == 1


def test_source_write_is_denied_before_the_file_is_created(tmp_path):
    path = tmp_path / "new-source"
    guard = _guard(tmp_path)
    audit._install_hook()
    current = audit._CURRENT.set(guard)
    try:
        with pytest.raises(PermissionError, match="business_write"):
            path.write_text("forbidden")
    finally:
        audit._CURRENT.reset(current)
    assert not path.exists() and guard.business_write_attempts == 1


def test_external_connect_is_refused_but_the_real_loopback_client_is_allowed(tmp_path):
    guard = _guard(tmp_path)
    guard.audit("socket.connect", (None, ("127.0.0.1", 1)))
    with pytest.raises(PermissionError, match="external_network"):
        guard.audit("socket.connect", (None, ("example.test", 443)))
    assert guard.external_network_attempts == 1


def test_writable_sqlite_uri_is_refused_without_reinterpreting_the_location(tmp_path):
    guard = _guard(tmp_path)
    with pytest.raises(PermissionError, match="write_connection"):
        guard.audit("sqlite3.connect", ((tmp_path / "source.sqlite3").as_uri() + "?mode=rw",))
    guard.audit("sqlite3.connect", ((tmp_path / "source.sqlite3").as_uri() + "?mode=ro",))
    assert guard.business_write_attempts == 1


def test_invalid_scope_is_rejected_before_building_a_system(tmp_path):
    with pytest.raises(ValueError, match="arguments_invalid"):
        audit.AuditCompanyHttp(Path("absent"), expected_publication_hash="a" * 64, codes=(), budget_seconds=1)
