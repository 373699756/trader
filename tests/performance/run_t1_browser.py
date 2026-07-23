from __future__ import annotations

import argparse
import json
import logging
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
sys.path.insert(0, str(PROJECT_ROOT))

from tests.performance.youhua_d4_browser_fixture import build_app  # noqa: E402
from trader.infra.settings import load_runtime_settings  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
LONG_SNAPSHOT_STATUS = (
    "已冻结 · 收盘补算 · 2026/7/23 17:29:49 · 降级：main:board_population_insufficient、"
    "main:board_data_reliability_below_threshold、chinext:board_population_insufficient、"
    "chinext:board_data_reliability_below_threshold、star:board_population_insufficient、"
    "star:board_data_reliability_below_threshold、deepseek_skipped_no_eligible_candidates"
)
LONG_RUNTIME_ERROR = (
    "TopK live overlay degraded: data_source_task exceeded its bounded deadline; the last valid projection "
    "remains visible while the source lane recovers"
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config" / "v2" / "runtime.json")
    args = parser.parse_args()
    output = args.output.resolve()
    report: dict[str, Any]
    try:
        budget = load_runtime_settings(args.config.resolve()).performance_budgets.latency_p95_ms[
            "browser_patch_to_paint"
        ]
        report = _run(budget)
    except Exception as exc:
        report = {
            "schema_version": "t1-browser-performance-v1",
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:500],
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


def _run(budget_p95_ms: float) -> dict[str, Any]:
    geckodriver = shutil.which("geckodriver")
    firefox = _firefox_binary()
    if geckodriver is None or firefox is None:
        raise RuntimeError("Firefox and geckodriver are required for the real browser performance gate")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app_port = _free_port()
    driver_port = _free_port()
    server = make_server("127.0.0.1", app_port, build_app(), threaded=True)
    server_thread = threading.Thread(target=server.serve_forever, name="t1-browser-fixture", daemon=True)
    server_thread.start()
    driver: subprocess.Popen[str] | None = None
    session_id = ""
    try:
        driver = subprocess.Popen(
            [geckodriver, "--host", "127.0.0.1", "--port", str(driver_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_driver(driver_port, driver)
        session = _request_json(
            f"http://127.0.0.1:{driver_port}/session",
            method="POST",
            timeout_seconds=30,
            payload={
                "capabilities": {
                    "alwaysMatch": {
                        "browserName": "firefox",
                        "moz:firefoxOptions": {"args": ["-headless"], "binary": firefox},
                    }
                }
            },
        )
        value = session.get("value")
        if not isinstance(value, dict) or not isinstance(value.get("sessionId"), str):
            raise RuntimeError("geckodriver did not return a W3C session id")
        session_id = value["sessionId"]
        base = f"http://127.0.0.1:{driver_port}/session/{session_id}"
        _request_json(
            f"{base}/url",
            method="POST",
            payload={"url": f"http://127.0.0.1:{app_port}/"},
        )
        _wait_script(
            base,
            "return Boolean(window.TraderDashboardDiagnostics"
            " && window.TraderDashboardDiagnostics.snapshot().recommendationFullResponses > 0);",
        )
        for _index in range(24):
            _request_json(f"http://127.0.0.1:{app_port}/__d4/publish", method="POST", payload={})
            time.sleep(0.05)
        _wait_script(
            base,
            "return window.TraderDashboardDiagnostics.snapshot().patchToPaint.sample_count >= 20;",
        )
        diagnostics = _execute(base, "return window.TraderDashboardDiagnostics.snapshot();")
        _execute(
            base,
            "document.getElementById('noticeText').textContent = "
            f"{json.dumps(LONG_SNAPSHOT_STATUS, ensure_ascii=False)};"
            "document.getElementById('lastError').textContent = "
            f"{json.dumps(LONG_RUNTIME_ERROR, ensure_ascii=False)};"
            "return true;",
        )
        viewport_results = []
        for width, height in VIEWPORTS:
            _request_json(
                f"{base}/window/rect",
                method="POST",
                payload={"width": width, "height": height, "x": 0, "y": 0},
            )
            time.sleep(0.1)
            layout = _execute(
                base,
                "return {"
                "width: window.innerWidth,"
                "height: window.innerHeight,"
                "overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,"
                "body: Boolean(document.body && document.body.getBoundingClientRect().height > 0),"
                "runtimeMessageHeights: Array.from(document.querySelectorAll('.runtime-message')).map((item) => item.offsetHeight),"
                "runtimeMessageOverflow: Array.from(document.querySelectorAll('.runtime-message b')).map((item) => getComputedStyle(item).overflowY),"
                "runtimeMessageScrollable: Array.from(document.querySelectorAll('.runtime-message b')).map((item) => item.scrollHeight > item.clientHeight),"
                "messagesAboveControls: document.querySelector('.runtime-messages').getBoundingClientRect().bottom <= document.querySelector('.control-band').getBoundingClientRect().top,"
                "summaryTouchesControls: document.querySelector('.summary-band').getBoundingClientRect().bottom === document.querySelector('.control-band').getBoundingClientRect().top,"
                "controlsTouchTable: document.querySelector('.control-band').getBoundingClientRect().bottom === document.querySelector('.table-region').getBoundingClientRect().top"
                "};",
            )
            viewport_results.append({"requested": [width, height], **layout})
        patch = diagnostics.get("patchToPaint") if isinstance(diagnostics, dict) else None
        p95 = patch.get("p95_ms") if isinstance(patch, dict) else None
        passed = (
            isinstance(p95, (int, float))
            and not isinstance(p95, bool)
            and p95 <= budget_p95_ms
            and all(
                item.get("body") is True
                and item.get("overflow") is False
                and item.get("runtimeMessageHeights") == [52, 52]
                and item.get("runtimeMessageOverflow") == ["auto", "auto"]
                and item.get("runtimeMessageScrollable") == [True, True]
                and item.get("messagesAboveControls") is True
                and item.get("summaryTouchesControls") is True
                and item.get("controlsTouchTable") is True
                for item in viewport_results
            )
        )
        return {
            "schema_version": "t1-browser-performance-v1",
            "passed": passed,
            "budget_p95_ms": budget_p95_ms,
            "patch_to_paint": patch,
            "patches_applied": diagnostics.get("recommendationPatchesApplied"),
            "resync_requests": diagnostics.get("resyncRequests"),
            "browser_errors": diagnostics.get("browserErrors"),
            "viewports": viewport_results,
            "network_calls": 0,
        }
    finally:
        if session_id:
            try:
                _request_json(
                    f"http://127.0.0.1:{driver_port}/session/{session_id}",
                    method="DELETE",
                )
            except (OSError, RuntimeError, urllib.error.URLError):
                pass
        if driver is not None:
            driver.terminate()
            try:
                driver.wait(timeout=5)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.wait(timeout=5)
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _firefox_binary() -> str | None:
    candidates = (
        Path("/snap/firefox/current/usr/lib/firefox/firefox"),
        Path(shutil.which("firefox") or ""),
    )
    for candidate in candidates:
        if candidate.is_file() and candidate.stat().st_mode & 0o111:
            return str(candidate)
    return None


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_driver(port: int, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            error = process.stderr.read() if process.stderr is not None else ""
            raise RuntimeError(f"geckodriver exited before readiness: {error[:300]}")
        try:
            response = _request_json(f"http://127.0.0.1:{port}/status")
        except (OSError, urllib.error.URLError, RuntimeError):
            time.sleep(0.05)
            continue
        value = response.get("value")
        if isinstance(value, dict) and value.get("ready") is True:
            return
        time.sleep(0.05)
    raise RuntimeError("geckodriver did not become ready within 10 seconds")


def _wait_script(base: str, script: str) -> None:
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if _execute(base, script) is True:
            return
        time.sleep(0.05)
    raise RuntimeError("browser condition did not become true within 15 seconds")


def _execute(base: str, script: str) -> Any:
    response = _request_json(
        f"{base}/execute/sync",
        method="POST",
        payload={"script": script, "args": []},
    )
    return response.get("value")


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 5,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"WebDriver or fixture returned HTTP {exc.code}: {detail[:500]}") from exc
    except (TimeoutError, urllib.error.URLError) as exc:
        raise RuntimeError(f"request failed for {url}: {exc}") from exc
    if not isinstance(raw, dict):
        raise RuntimeError("WebDriver or fixture response must be a JSON object")
    if isinstance(raw.get("value"), dict) and raw["value"].get("error"):
        raise RuntimeError(str(raw["value"].get("message") or raw["value"]["error"]))
    return raw


if __name__ == "__main__":
    raise SystemExit(main())
