from __future__ import annotations

import argparse
import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from werkzeug.serving import make_server

try:
    import websocket
except ImportError as exc:  # pragma: no cover
    websocket = None  # type: ignore[assignment]
    _WEBSOCKET_IMPORT_ERROR = exc
else:
    _WEBSOCKET_IMPORT_ERROR = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tests.performance.pipeline_d4_browser_fixture import build_app  # noqa: E402
from trader.web.static_assets import WEB_ASSET_REVISION  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "unified-v2-browser-v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    try:
        report = _run()
    except Exception as exc:
        report = {
            "schema_version": REPORT_SCHEMA,
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def _run() -> dict[str, object]:
    if websocket is None:
        raise RuntimeError("websocket-client is required for the Chrome dashboard gate") from _WEBSOCKET_IMPORT_ERROR
    chrome = _chrome_binary()
    if chrome is None:
        raise RuntimeError("Google Chrome or Chromium is required")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app_port = _free_port()
    debug_port = _free_port()
    server = make_server("127.0.0.1", app_port, build_app(), threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, name="unified-v2-browser-fixture", daemon=True)
    server_thread.start()
    browser: subprocess.Popen[str] | None = None
    socket_client: websocket.WebSocket | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="trader-v2-chrome-") as profile_dir:
            browser = subprocess.Popen(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-background-networking",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={debug_port}",
                    "--remote-allow-origins=*",
                    f"--user-data-dir={profile_dir}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_chrome(debug_port, browser)
            target = f"http://127.0.0.1:{app_port}/"
            tab = _request_json(f"http://127.0.0.1:{debug_port}/json/new?{target}", method="PUT")
            socket_client = websocket.create_connection(str(tab["webSocketDebuggerUrl"]), timeout=5)
            cdp = _CdpSession(socket_client)
            cdp.call("Runtime.enable")
            cdp.call("Page.enable")
            cdp.call("Page.navigate", {"url": target})
            _wait(
                lambda: (
                    _evaluate(
                        cdp,
                        "Boolean(window.TraderV2Diagnostics && document.querySelectorAll('#decisionRows tr[data-code]').length)",
                    )
                    is True
                )
            )
            _request_json(f"http://127.0.0.1:{app_port}/__v2/publish", method="POST", payload={})
            _wait(lambda: _request_json(f"http://127.0.0.1:{app_port}/__v2/metrics").get("current_gets", 0) >= 2)
            viewports = [_viewport(cdp, width, height) for width, height in VIEWPORTS]
            _evaluate(cdp, "document.querySelector('button[data-strategy=\"long\"]').click()")
            _wait(
                lambda: (
                    _evaluate(
                        cdp,
                        "document.getElementById('panelTitle').textContent.includes('Long') && document.querySelectorAll('#decisionRows tr[data-code]').length > 0",
                    )
                    is True
                )
            )
            long_viewports = [_viewport(cdp, width, height) for width, height in VIEWPORTS]
            scripts = _evaluate(cdp, "Array.from(document.scripts).map((item)=>item.src)")
            expected_script = f"/static/dashboard.js?rev={WEB_ASSET_REVISION}"
            passed = (
                isinstance(scripts, list)
                and len(scripts) == 1
                and expected_script in scripts[0]
                and all(_viewport_passed(item) for item in (*viewports, *long_viewports))
            )
            return {
                "schema_version": REPORT_SCHEMA,
                "passed": passed,
                "viewports": viewports,
                "long_viewports": long_viewports,
                "scripts": scripts,
                "external_network_calls": 0,
            }
    finally:
        if socket_client is not None:
            socket_client.close()
        if browser is not None:
            browser.terminate()
            try:
                browser.wait(timeout=5)
            except subprocess.TimeoutExpired:
                browser.kill()
                browser.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


class _CdpSession:
    def __init__(self, socket_client: Any) -> None:
        self._socket = socket_client
        self._counter = 0

    def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        self._counter += 1
        self._socket.send(json.dumps({"id": self._counter, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") == self._counter:
                return message


def _viewport(cdp: _CdpSession, width: int, height: int) -> dict[str, object]:
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
    )
    time.sleep(0.1)
    result = _evaluate(
        cdp,
        "(()=>{const header=document.querySelector('.app-header').getBoundingClientRect();"
        "const toolbar=document.querySelector('.toolbar').getBoundingClientRect();"
        "const metrics=document.querySelector('.metric-grid').getBoundingClientRect();"
        "const content=document.querySelector('.content-grid').getBoundingClientRect();"
        "return {body:Boolean(document.body&&document.body.getBoundingClientRect().height>0),"
        "overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,"
        "ordered:header.bottom<=toolbar.top&&toolbar.bottom<=metrics.top&&metrics.bottom<=content.top,"
        "rows:document.querySelectorAll('#decisionRows tr[data-code]').length,"
        "browserErrors:window.TraderV2Diagnostics.errors};})()",
    )
    return {"requested": [width, height], **(result if isinstance(result, dict) else {})}


def _viewport_passed(item: dict[str, object]) -> bool:
    return bool(
        item.get("body")
        and not item.get("overflow")
        and item.get("ordered")
        and isinstance(item.get("rows"), int)
        and int(item["rows"]) > 0
        and item.get("browserErrors") == []
    )


def _evaluate(cdp: _CdpSession, expression: str) -> object:
    response = cdp.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
    return response.get("result", {}).get("result", {}).get("value")


def _wait(condition: Any) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise RuntimeError("browser condition timed out")


def _chrome_binary() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser", "chrome"):
        candidate = shutil.which(name)
        if candidate:
            return candidate
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_chrome(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"Chrome exited before readiness: {error[:300]}")
        try:
            if _request_json(f"http://127.0.0.1:{port}/json/version").get("webSocketDebuggerUrl"):
                return
        except (OSError, RuntimeError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise RuntimeError("Chrome DevTools did not become ready")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    with urllib.request.urlopen(request, timeout=5) as response:
        parsed = json.loads(response.read().decode())
    if not isinstance(parsed, dict):
        raise RuntimeError("JSON response must be an object")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
