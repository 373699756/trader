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
except ImportError as exc:  # pragma: no cover - exercised only in incomplete dev environments.
    websocket = None  # type: ignore[assignment]
    _WEBSOCKET_IMPORT_ERROR = exc
else:
    _WEBSOCKET_IMPORT_ERROR = None

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tests.performance.pipeline_d4_browser_fixture import build_app  # noqa: E402
from trader.infra.settings import load_runtime_settings  # noqa: E402
from trader.web.static_assets import WEB_ASSET_REVISION  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "chrome-dashboard-performance-v1"
PATCH_SAMPLE_TARGET = 20


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "v2" / "runtime.json")
    args = parser.parse_args()
    output = args.output.resolve()
    try:
        budget = load_runtime_settings(args.config.resolve()).performance_budgets.latency_p95_ms[
            "browser_patch_to_paint"
        ]
        report = _run(budget)
    except Exception as exc:
        report = {
            "schema_version": REPORT_SCHEMA,
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def _run(budget_p95_ms: float) -> dict[str, Any]:
    if websocket is None:
        raise RuntimeError("websocket-client is required for the Chrome DevTools dashboard gate") from (
            _WEBSOCKET_IMPORT_ERROR
        )
    chrome = _chrome_binary()
    if chrome is None:
        raise RuntimeError("Google Chrome or Chromium is required for the Chrome dashboard gate")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app_port = _free_port()
    debug_port = _free_port()
    server = make_server("127.0.0.1", app_port, build_app(), threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, name="chrome-dashboard-fixture", daemon=True)
    server_thread.start()
    browser: subprocess.Popen[str] | None = None
    socket_client: websocket.WebSocket | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="trader-chrome-profile-") as profile_dir:
            browser = subprocess.Popen(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-background-networking",
                    "--remote-debugging-address=127.0.0.1",
                    f"--remote-debugging-port={debug_port}",
                    f"--remote-allow-origins=http://127.0.0.1:{debug_port}",
                    f"--user-data-dir={profile_dir}",
                    "about:blank",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            _wait_chrome(debug_port, browser)
            target_url = f"http://127.0.0.1:{app_port}/"
            tab = _request_json(f"http://127.0.0.1:{debug_port}/json/new?{target_url}", method="PUT")
            socket_client = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=5)
            cdp = _CdpSession(socket_client)
            cdp.call("Runtime.enable")
            cdp.call("Page.enable")
            cdp.call("Page.navigate", {"url": target_url})
            _wait_page_ready(cdp)
            for _index in range(24):
                _request_json(f"http://127.0.0.1:{app_port}/__d4/publish", method="POST", payload={})
                time.sleep(0.05)
            _wait_patch_samples(cdp)
            diagnostics = _evaluate(cdp, "window.TraderDashboardDiagnostics.snapshot();")
            scripts = _evaluate(cdp, "Array.from(document.scripts).map((script)=>script.src);")
            viewport_results = [_check_viewport(cdp, width, height) for width, height in VIEWPORTS]
            patch = diagnostics.get("patchToPaint") if isinstance(diagnostics, dict) else None
            p95 = patch.get("p95_ms") if isinstance(patch, dict) else None
            passed = (
                isinstance(p95, (int, float))
                and not isinstance(p95, bool)
                and p95 <= budget_p95_ms
                and _scripts_use_current_revision(scripts)
                and diagnostics.get("browserErrors") == []
                and all(_viewport_passed(item) for item in viewport_results)
            )
            return {
                "schema_version": REPORT_SCHEMA,
                "passed": passed,
                "budget_p95_ms": budget_p95_ms,
                "patch_to_paint": patch,
                "patches_applied": diagnostics.get("recommendationPatchesApplied"),
                "resync_requests": diagnostics.get("resyncRequests"),
                "browser_errors": diagnostics.get("browserErrors"),
                "scripts": scripts,
                "viewports": viewport_results,
                "network_calls": 0,
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
    def __init__(self, socket_client: websocket.WebSocket) -> None:
        self._socket = socket_client
        self._counter = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._counter += 1
        self._socket.send(json.dumps({"id": self._counter, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") == self._counter:
                return message


def _check_viewport(cdp: _CdpSession, width: int, height: int) -> dict[str, Any]:
    cdp.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
    )
    time.sleep(0.1)
    layout = _evaluate(
        cdp,
        "({width:window.innerWidth,height:window.innerHeight,"
        "overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,"
        "body:Boolean(document.body&&document.body.getBoundingClientRect().height>0),"
        "messagesAboveControls:document.querySelector('.runtime-messages').getBoundingClientRect().bottom"
        "<=document.querySelector('.control-band').getBoundingClientRect().top,"
        "summaryAboveControls:document.querySelector('.summary-band').getBoundingClientRect().bottom"
        "<=document.querySelector('.control-band').getBoundingClientRect().top,"
        "controlsAboveTable:document.querySelector('.control-band').getBoundingClientRect().bottom"
        "<=document.querySelector('.table-region').getBoundingClientRect().top,"
        "browserErrors:window.TraderDashboardDiagnostics.snapshot().browserErrors})",
    )
    return {"requested": [width, height], **layout}


def _viewport_passed(item: dict[str, Any]) -> bool:
    return bool(
        item.get("body")
        and not item.get("overflow")
        and item.get("messagesAboveControls")
        and item.get("summaryAboveControls")
        and item.get("controlsAboveTable")
        and item.get("browserErrors") == []
    )


def _scripts_use_current_revision(scripts: object) -> bool:
    if not isinstance(scripts, list) or not scripts:
        return False
    return all(isinstance(item, str) and f"?rev={WEB_ASSET_REVISION}" in item for item in scripts)


def _wait_page_ready(cdp: _CdpSession) -> None:
    _wait_condition(
        lambda: (
            _evaluate(
                cdp,
                "Boolean(window.TraderDashboardDiagnostics"
                " && window.TraderDashboardPatches"
                " && window.TraderDashboardDiagnostics.snapshot().recommendationFullResponses > 0)",
            )
            is True
        ),
        "dashboard did not publish the initial recommendation response",
    )


def _wait_patch_samples(cdp: _CdpSession) -> None:
    _wait_condition(
        lambda: (
            _evaluate(
                cdp,
                "window.TraderDashboardDiagnostics.snapshot().patchToPaint.sample_count",
            )
            >= PATCH_SAMPLE_TARGET
        ),
        "dashboard did not collect enough patch-to-paint samples",
    )


def _wait_condition(condition: Any, message: str) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise RuntimeError(message)


def _evaluate(cdp: _CdpSession, expression: str) -> Any:
    response = cdp.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": True},
    )
    return response.get("result", {}).get("result", {}).get("value")


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
            response = _request_json(f"http://127.0.0.1:{port}/json/version")
        except (OSError, RuntimeError, urllib.error.URLError):
            time.sleep(0.05)
            continue
        if isinstance(response.get("webSocketDebuggerUrl"), str):
            return
        time.sleep(0.05)
    raise RuntimeError("Chrome DevTools did not become ready within 10 seconds")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 5,
) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if payload is not None else {},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        decoded = response.read().decode("utf-8")
    parsed = json.loads(decoded)
    if not isinstance(parsed, dict):
        raise RuntimeError("JSON response must be an object")
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
