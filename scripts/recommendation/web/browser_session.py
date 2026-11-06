"""Owned Chrome process family and bounded DevTools commands for browser diagnostics."""

from __future__ import annotations

import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from typing import Protocol, cast

import websocket

from trader.infra.http_contracts import HttpCancelled, HttpError, HttpTransport
from trader.infra.process_family import ProcessFamily


class _BrowserProfile(Protocol):
    def cleanup(self) -> None: ...


class ChromeSession:
    def __init__(self, binary: str, port: int, *, open_http: Callable[[], HttpTransport]) -> None:
        self._open_http = open_http
        self._next_id = 0
        self._profile = tempfile.TemporaryDirectory(prefix="trader-desktop-chrome-")
        self._process: ProcessFamily | None = None
        self._socket: websocket.WebSocket | None = None
        self._closed = False
        try:
            self._process = ProcessFamily(
                (
                    binary,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--remote-allow-origins=*",
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={self._profile.name}",
                    "about:blank",
                ),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            debugger_url = self._wait_debugger_url(port)
            self._socket = websocket.create_connection(debugger_url, timeout=15)
            self._command("Runtime.enable")
            self._command("Page.enable")
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> ChromeSession:
        if self._closed or self._process is None or self._socket is None:
            raise RuntimeError("Chrome session is closed")
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _wait_debugger_url(self, port: int) -> str:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self._process is None or self._process.poll() is not None:
                raise RuntimeError("Chrome exited before readiness")
            try:
                with self._open_http() as transport:
                    response = transport.get(
                        f"http://127.0.0.1:{port}/json/list", timeout=1, max_response_bytes=1024 * 1024,
                    )
                    response.raise_for_status()
                    targets = response.json()
                if isinstance(targets, list):
                    for target in targets:
                        if not isinstance(target, dict) or target.get("type") != "page":
                            continue
                        debugger_url = target.get("webSocketDebuggerUrl")
                        if isinstance(debugger_url, str):
                            return debugger_url
            except (HttpCancelled, InterruptedError):
                raise
            except (HttpError, json.JSONDecodeError):
                pass
            time.sleep(0.05)
        raise RuntimeError("Chrome DevTools endpoint did not become ready")

    def _command(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        connection = self._socket
        if connection is None or self._process is None:
            raise RuntimeError("Chrome session is closed")
        self._next_id += 1
        command_id = self._next_id
        deadline = time.monotonic() + 15
        connection.settimeout(15)
        connection.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Chrome DevTools command timed out")
            connection.settimeout(remaining)
            parsed: object = json.loads(connection.recv())
            if not isinstance(parsed, dict):
                continue
            response = cast(dict[str, object], parsed)
            if response.get("id") != command_id:
                continue
            if "error" in response:
                raise RuntimeError("Chrome DevTools command failed")
            result = response.get("result", {})
            return cast(dict[str, object], result) if isinstance(result, dict) else {}

    def navigate(self, url: str) -> None:
        self._command("Page.navigate", {"url": url})

    def execute(self, script: str) -> object:
        response = self._command(
            "Runtime.evaluate",
            {
                "expression": f"(function(){{{script}}})()",
                "awaitPromise": True,
                "returnByValue": True,
            },
        )
        if response.get("exceptionDetails"):
            raise RuntimeError("Chrome JavaScript evaluation failed")
        result = response.get("result", {})
        return cast(dict[str, object], result).get("value") if isinstance(result, dict) else None

    def set_viewport(self, width: int, height: int) -> None:
        self._command(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def screenshot(self) -> object:
        return self._command("Page.captureScreenshot", {"format": "png", "fromSurface": True}).get("data")

    def close(self) -> None:
        if self._closed:
            return
        failure: BaseException | None = None
        if self._socket is not None:
            try:
                # The owned Chrome family is terminated next; no remote close
                # handshake may extend this scope's cleanup.
                self._socket.shutdown()
                self._socket = None
            except BaseException as exc:
                failure = exc
        if self._process is not None:
            try:
                self._process.close()
            except BaseException as exc:
                if failure is None:
                    failure = exc
            else:
                try:
                    self._process.close_streams()
                    self._process = None
                except BaseException as exc:
                    if failure is None:
                        failure = exc
        if self._process is None:
            try:
                _cleanup_browser_profile(self._profile)
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if failure is not None:
            raise failure
        self._closed = True


def _cleanup_browser_profile(profile: _BrowserProfile, *, attempts: int = 20, delay_seconds: float = 0.05) -> None:
    for attempt in range(attempts):
        try:
            profile.cleanup()
            return
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay_seconds)


__all__ = ["ChromeSession"]
