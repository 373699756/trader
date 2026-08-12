from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.web import create_app  # noqa: E402
from trader.web.static_assets import WEB_ASSET_REVISION  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "v2-desktop-browser-v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = _run(args.output.parent)
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


def _run(output_dir: Path) -> dict[str, object]:
    driver_binary = shutil.which("geckodriver")
    if driver_binary is None or shutil.which("firefox") is None:
        raise RuntimeError("Firefox and geckodriver are required for the desktop gate")
    app_port = _free_port()
    driver_port = _free_port()
    server = make_server("127.0.0.1", app_port, create_app(), threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, name="v2-browser-fixture", daemon=True)
    server_thread.start()
    driver = subprocess.Popen(
        [driver_binary, "--host", "127.0.0.1", "--port", str(driver_port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    session_id: str | None = None
    try:
        _wait_driver(driver_port, driver)
        created = _request_json(
            f"http://127.0.0.1:{driver_port}/session",
            method="POST",
            payload={
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "firefox",
                        "moz:firefoxOptions": {"args": ["-headless"]},
                    }
                }
            },
        )
        session_id = str(created["value"]["sessionId"])
        base = f"http://127.0.0.1:{driver_port}/session/{session_id}"
        _request_json(f"{base}/url", method="POST", payload={"url": f"http://127.0.0.1:{app_port}/"})
        _wait(lambda: bool(_execute(base, "return Boolean(window.TraderDashboardDiagnostics);")))
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=long]").click(); return true;')
        _wait(lambda: int(_execute(base, 'return document.querySelectorAll("#tableBody tr[data-code]").length;')) > 0)
        output_dir.mkdir(parents=True, exist_ok=True)
        viewports = [_viewport(base, output_dir, width, height) for width, height in VIEWPORTS]
        scripts = _execute(base, "return Array.from(document.scripts).map((item) => item.src);")
        expected = f"/static/dashboard.js?rev={WEB_ASSET_REVISION}"
        passed = (
            isinstance(scripts, list)
            and any(expected in str(script) for script in scripts)
            and all(_viewport_passed(viewport) for viewport in viewports)
        )
        return {
            "schema_version": REPORT_SCHEMA,
            "passed": passed,
            "browser": "firefox-headless",
            "viewports": viewports,
            "scripts": scripts,
            "external_network_calls": 0,
        }
    finally:
        if session_id is not None:
            try:
                _request_json(
                    f"http://127.0.0.1:{driver_port}/session/{session_id}",
                    method="DELETE",
                )
            except (OSError, RuntimeError, urllib.error.URLError):
                pass
        driver.terminate()
        try:
            driver.wait(timeout=5)
        except subprocess.TimeoutExpired:
            driver.kill()
            driver.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _viewport(base: str, output_dir: Path, width: int, height: int) -> dict[str, object]:
    _set_viewport(base, width, height)
    time.sleep(0.2)
    result = _execute(
        base,
        """
        const header = document.querySelector('.app-header').getBoundingClientRect();
        const summary = document.querySelector('.summary-band').getBoundingClientRect();
        const controls = document.querySelector('.control-band').getBoundingClientRect();
        const layout = document.querySelector('#recommendation-layout').getBoundingClientRect();
        const sidebar = document.querySelector('#long-sidebar').getBoundingClientRect();
        const table = document.querySelector('.table-region').getBoundingClientRect();
        return {
          actual: [window.innerWidth, window.innerHeight],
          body: Boolean(document.body && document.body.getBoundingClientRect().height > 0),
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
          ordered: header.bottom <= summary.top && summary.bottom <= controls.top && controls.bottom <= layout.top,
          longVisible: !document.querySelector('#long-sidebar').hidden && !document.querySelector('#longScopeTabs').hidden,
          noLongOverlap: sidebar.right <= table.left,
          rows: document.querySelectorAll('#tableBody tr[data-code]').length,
          scopes: document.querySelectorAll('#longScopeTabs button[data-scope]').length,
          notice: document.querySelector('#noticeText').textContent,
          browserErrors: window.TraderDashboardDiagnostics.snapshot().browserErrors,
        };
        """,
    )
    screenshot = _request_json(f"{base}/screenshot")["value"]
    screenshot_name = f"desktop-{width}x{height}.png"
    (output_dir / screenshot_name).write_bytes(base64.b64decode(str(screenshot)))
    return {"requested": [width, height], "screenshot": screenshot_name, **dict(result)}


def _set_viewport(base: str, width: int, height: int) -> None:
    _request_json(
        f"{base}/window/rect",
        method="POST",
        payload={"x": 0, "y": 0, "width": width, "height": height},
    )
    actual = _execute(base, "return [window.innerWidth, window.innerHeight];")
    if not isinstance(actual, list) or len(actual) != 2:
        raise RuntimeError("browser did not report its viewport")
    _request_json(
        f"{base}/window/rect",
        method="POST",
        payload={
            "x": 0,
            "y": 0,
            "width": width + width - int(actual[0]),
            "height": height + height - int(actual[1]),
        },
    )


def _viewport_passed(result: dict[str, object]) -> bool:
    return bool(
        result.get("body")
        and result.get("actual") == result.get("requested")
        and not result.get("overflow")
        and result.get("ordered")
        and result.get("longVisible")
        and result.get("noLongOverlap")
        and result.get("rows")
        and result.get("scopes") == 3
        and result.get("browserErrors") == []
    )


def _execute(base: str, script: str) -> object:
    response = _request_json(f"{base}/execute/sync", method="POST", payload={"script": script, "args": []})
    return response.get("value")


def _wait_driver(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"geckodriver exited before readiness: {error[:300]}")
        try:
            if _request_json(f"http://127.0.0.1:{port}/status").get("value"):
                return
        except (OSError, RuntimeError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise RuntimeError("geckodriver did not become ready")


def _wait(condition: Any) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise RuntimeError("browser condition timed out")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            parsed = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"WebDriver HTTP {exc.code}: {detail[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("WebDriver response must be an object")
    if parsed.get("value") and isinstance(parsed["value"], dict) and parsed["value"].get("error"):
        raise RuntimeError(str(parsed["value"]))
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
