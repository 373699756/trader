"""Transport diagnostics retain one bounded cleanup deadline for both resources."""

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.download.market import market_factor_inputs, tencent_quotes
from tests.helpers.shared.configuration import RuntimeSettingsFixture, SqlitePolicyFixture
from trader import bootstrap
from trader.infra import settings as settings_module
from trader.infra.shutdown import ShutdownStep


@pytest.mark.parametrize("failure", (None, "startup", "shutdown"))
def test_transport_audit_uses_composed_resources_and_reports_incomplete_shutdown(monkeypatch, failure):
    events = []
    deadlines = []
    transports = []

    class Executor:
        def __init__(self, name):
            self.name = name

        def start(self):
            events.append((self.name, "start"))
            if failure == "startup" and self.name == "http":
                raise RuntimeError("fixture_startup_failed")
            return True

        def stop(self, *, deadline, wait=True, cancel_futures=False):
            events.append((self.name, "stop"))
            deadlines.append(deadline)
            if self.name == "workers":
                assert wait and cancel_futures
            return ShutdownStep(self.name, failure != "shutdown", False)

        def execute(self, request):
            raise AssertionError("supplier requests must not start")

    workers, http = Executor("workers"), Executor("http")

    def build_workers(count):
        assert count == 3
        return workers

    def build_http(count):
        assert count == 3
        return http

    def open_transport(*, observe, execute_request):
        transports.append(execute_request)
        return SimpleNamespace()

    class Client:
        def __init__(self, *, worker_pool, session_factory, timeout_seconds):
            assert worker_pool is workers and timeout_seconds == 3
            session_factory()

        def fetch_market(self, codes, *, deadline, request_attempts):
            assert codes == ("600001",)
            return ()

    monkeypatch.setattr(bootstrap, "BuildPerformanceExecutor", build_workers)
    monkeypatch.setattr(bootstrap, "BuildHttpExecutor", build_http)
    monkeypatch.setattr(bootstrap, "OpenExternalHttp", open_transport)
    monkeypatch.setattr(settings_module, "LoadRuntimeSettings", lambda _: RuntimeSettingsFixture())
    monkeypatch.setattr(market_factor_inputs, "ReadReferences", lambda *_args, **_kwargs: SimpleNamespace(
        observations=(SimpleNamespace(subject_key="600001"),),
    ))
    monkeypatch.setattr(tencent_quotes, "TencentClient", Client)
    arguments = Namespace(runtime_config=Path("unused.json"), timeout_seconds=3)

    if failure is None:
        report = tencent_quotes._audit_transport(arguments, sqlite_policy=SqlitePolicyFixture())
        assert report["status"] == "degraded"
        assert transports == [None, http.execute]
    else:
        reason = "fixture_startup_failed" if failure == "startup" else "shutdown_incomplete"
        with pytest.raises(RuntimeError, match=reason):
            tencent_quotes._audit_transport(arguments, sqlite_policy=SqlitePolicyFixture())

    assert events == [("workers", "start"), ("http", "start"), ("workers", "stop"), ("http", "stop")]
    assert len(deadlines) == 2 and deadlines[0] is deadlines[1]
