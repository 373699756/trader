"""Supplier IPC preserves bounded cause metadata without remote exception payloads."""

from __future__ import annotations

import errno
import multiprocessing
import pickle
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from trader.download.domain.baostock_daily import BaoStockSecurity
from trader.download.infra import baostock_sync_supplier as supplier_module
from trader.download.infra.baostock_session import LoginBaostock
from trader.download.infra.download_failure_diagnostics import DownloadFailureDiagnostics
from trader.download.infra.history_sync_failure import (
    BuildHistoryFailureEvidence,
    HistoryFailureCause,
    HistoryFailureCauses,
    HistoryFailureDetail,
    HistoryFailureEvidence,
    HistoryRemoteFailureError,
)

_PRIVATE = "private token=https://private.invalid/account full supplier payload"


class _Sdk:
    __version__ = "fixture"

    def __init__(self, stage):
        self.stage = stage

    def login(self):
        if self.stage == "login":
            raise ConnectionResetError(errno.ECONNRESET, _PRIVATE)
        return SimpleNamespace(error_code="0")

    def logout(self):
        return None

    def query_trade_dates(self, **_kwargs):
        raise OSError(errno.EHOSTUNREACH, _PRIVATE)


def _worker_with_isolated_failure(connection, stage):
    supplier_module.LoadBaostockSdk = lambda: _Sdk(stage)
    supplier_module._silence_vendor_output = lambda: None
    supplier_module._worker_main(connection, 0.01)


def _configuration(tmp_path):
    from tests.helpers.download.configuration import HistorySyncConfigurationFixture

    return HistorySyncConfigurationFixture(
        tmp_path / "history", tmp_path / "reference", supplier_retries=0,
    )


def _login_evidence():
    try:
        LoginBaostock(_Sdk("login"))
    except RuntimeError as exc:
        return BuildHistoryFailureEvidence(exc)
    raise AssertionError("fixture login unexpectedly succeeded")


def test_evidence_contains_only_typed_safe_metadata_and_bounds_context_cycles():
    evidence = _login_evidence()
    assert [cause.exception_type for cause in evidence.causes] == ["RuntimeError", "ConnectionResetError"]
    assert evidence.causes[1].error_number == errno.ECONNRESET
    assert _PRIVATE.encode() not in pickle.dumps(evidence)
    remote = HistoryRemoteFailureError("supplier_login_network_failed", evidence)
    assert "ConnectionResetError:errno_" in HistoryFailureDetail(remote)
    assert "private.invalid" not in HistoryFailureDetail(remote)
    assert "https" not in repr(evidence)
    first, second = RuntimeError(_PRIVATE), OSError(errno.EIO, _PRIVATE)
    first.__context__, second.__context__ = second, first
    assert len(BuildHistoryFailureEvidence(first).causes) == 2
    chain = first
    for _ in range(10):
        outer = RuntimeError(_PRIVATE)
        outer.__cause__ = chain
        chain = outer
    assert len(BuildHistoryFailureEvidence(chain).causes) == 4


@pytest.mark.parametrize("constructor", (supplier_module._Ready, supplier_module._Response))
def test_protocol_rejects_a_failure_code_without_its_evidence(constructor):
    with pytest.raises(ValueError, match="supplier_failure_evidence_required"):
        constructor(failure_reason="supplier_failed")
    with pytest.raises(ValueError, match="supplier_failure_evidence_required"):
        constructor(failure_evidence=HistoryFailureEvidence((HistoryFailureCause("OSError", errno.EIO),)))


@pytest.mark.parametrize("stage", ("login", "query"))
def test_real_spawn_sends_evidence_instead_of_login_or_query_exception_payloads(stage):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_worker_with_isolated_failure, args=(child, stage))
    process.start()
    child.close()
    try:
        assert parent.poll(10), "supplier fixture did not send readiness"
        response = parent.recv()
        assert isinstance(response, supplier_module._Ready)
        if stage == "query":
            assert response.failure_reason is None and response.failure_evidence is None
            parent.send(supplier_module._LoadContext(date(2026, 10, 9), 2, ()))
            while True:
                assert parent.poll(10), "supplier fixture did not return query failure"
                response = parent.recv()
                if isinstance(response, supplier_module._Response):
                    break
                assert isinstance(response, supplier_module._Activity)
            parent.send(supplier_module._Stop())
        assert response.failure_evidence is not None
        cause = response.failure_evidence.causes[-1]
        assert (cause.exception_type, cause.error_number) == (
            ("ConnectionResetError", errno.ECONNRESET) if stage == "login" else ("OSError", errno.EHOSTUNREACH)
        )
        expected_reason = "supplier_login_network_failed" if stage == "login" else "supplier_failed"
        assert response.failure_reason == expected_reason
        assert _PRIVATE.encode() not in pickle.dumps(response)
        process.join(10)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.kill()
        process.join(3)
        parent.close()
        process.close()


def test_parent_login_handshake_restores_remote_evidence_before_recording_failure(tmp_path, monkeypatch):
    response = supplier_module._Ready("supplier_login_network_failed", _login_evidence())

    class Process:
        running = True

        def start(self):
            pass

        def is_alive(self):
            return self.running

        def join(self, **_kwargs):
            pass

        def terminate(self):
            self.running = False

        def kill(self):
            self.running = False

    process = Process()

    class Connection:
        def poll(self, _timeout):
            return True

        def recv(self):
            return response

        def send(self, _command):
            process.running = False

        def close(self):
            pass

    connection = Connection()
    context = SimpleNamespace(Pipe=lambda: (connection, Connection()), Process=lambda **_: process)
    monkeypatch.setattr(supplier_module, "get_context", lambda _: context)
    supplier = supplier_module.BaoStockHistorySupplier(_configuration(tmp_path))
    try:
        with pytest.raises(HistoryRemoteFailureError, match="supplier_login_network_failed") as failure:
            supplier._start(supplier_module._Attempt(1, 1))
        assert failure.value.evidence == response.failure_evidence
        assert "ConnectionResetError" in HistoryFailureDetail(failure.value)
        assert not process.running
    finally:
        supplier.close()


def test_query_observer_and_final_error_share_the_remote_evidence(tmp_path, monkeypatch):
    persisted = []
    evidence = _login_evidence()
    diagnostics = DownloadFailureDiagnostics(persisted.append, lambda: datetime(2026, 10, 9, tzinfo=ZoneInfo("Asia/Shanghai")))
    supplier = supplier_module.BaoStockHistorySupplier(_configuration(tmp_path), failures=diagnostics)
    response = supplier_module._Response(failure_reason="qfq_daily_query_failed", failure_evidence=evidence)
    monkeypatch.setattr(supplier, "_request_once", lambda _command, state, _attempt, _timeout: supplier_module._AttemptResult(response, state, response.failure_reason))
    security = BaoStockSecurity("600001", "fixture", "main", date(2000, 1, 1), None, "fixture")
    try:
        with pytest.raises(RuntimeError, match="qfq_daily_query_failed") as failure:
            supplier.fetch_code(security, (date(2026, 10, 9),))
        assert isinstance(failure.value.__cause__, HistoryRemoteFailureError)
        assert failure.value.__cause__.evidence == evidence
        assert len(persisted) == 1 and persisted[0].reason == "qfq_daily_query_failed"
        assert persisted[0].causes == " > ".join(HistoryFailureCauses(evidence))
        assert "ConnectionResetError:errno_" in persisted[0].diagnostic
        assert _PRIVATE not in persisted[0].diagnostic and "private.invalid" not in persisted[0].causes
    finally:
        supplier.close()
