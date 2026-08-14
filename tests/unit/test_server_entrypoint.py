from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from flask import Flask

from trader.entrypoints import server as server_entrypoint
from trader.infra.settings import RuntimeSettings


class _Controller:
    deadline = None

    def __init__(self, **_kwargs: object) -> None:
        pass

    def install(self) -> None:
        pass

    def restore(self) -> None:
        pass


class _System:
    def __init__(self) -> None:
        self.settings = cast(
            RuntimeSettings,
            SimpleNamespace(server=SimpleNamespace(host="127.0.0.1", port=5000)),
        )
        self.app = Flask(__name__)

    def start(self) -> bool:
        return True

    def stop(self) -> None:
        pass


def test_run_system_prints_bound_address_before_web_serving(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    system = _System()
    bound_server = object()
    observed_output: list[str] = []

    monkeypatch.setattr(server_entrypoint, "ShutdownSignalController", _Controller)
    monkeypatch.setattr(server_entrypoint, "make_server", lambda *_args, **_kwargs: bound_server)

    def serve(*_args: object, **_kwargs: object) -> int:
        observed_output.append(capsys.readouterr().out)
        return 0

    monkeypatch.setattr(server_entrypoint, "_serve_with_controller", serve)

    assert server_entrypoint._run_system(system, timeout_seconds=30.0) == 0
    assert observed_output == ["127.0.0.1:5000\n"]
