from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
from pathlib import Path
from typing import Any

from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tests.performance.run_t1_browser import (  # noqa: E402
    _execute,
    _firefox_binary,
    _free_port,
    _request_json,
    _wait_driver,
    _wait_script,
)
from tests.performance.tomorrow_v2_browser_fixture import build_app  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "tomorrow-v2-browser-v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
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
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0 if report["passed"] else 1


def _run() -> dict[str, object]:
    geckodriver = shutil.which("geckodriver")
    firefox = _firefox_binary()
    if geckodriver is None or firefox is None:
        raise RuntimeError("Firefox and geckodriver are required")
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    app_port = _free_port()
    driver_port = _free_port()
    server = make_server("127.0.0.1", app_port, build_app(), threaded=True)
    server_thread = threading.Thread(
        target=server.serve_forever,
        name="tomorrow-v2-browser-fixture",
        daemon=True,
    )
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
                        "moz:firefoxOptions": {
                            "args": ["-headless"],
                            "binary": firefox,
                        },
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
            payload={"url": f"http://127.0.0.1:{app_port}/v2/tomorrow"},
        )
        _wait_script(
            base,
            "return document.querySelectorAll('#decisionRows tr[data-code]').length > 0"
            " && document.getElementById('streamState').textContent === '在线';",
        )
        before = _request_json(f"http://127.0.0.1:{app_port}/__tomorrow_v2/metrics")
        _request_json(
            f"http://127.0.0.1:{app_port}/__tomorrow_v2/overlay",
            method="POST",
            payload={},
        )
        _wait_script(
            base,
            "return document.querySelector('#decisionRows tr[data-code]').textContent.includes('13.37');",
        )
        after = _request_json(f"http://127.0.0.1:{app_port}/__tomorrow_v2/metrics")
        viewport_results = [_viewport(base, width, height) for width, height in VIEWPORTS]
        full_gets_unchanged = before.get("current_gets") == after.get("current_gets") == 1
        passed = full_gets_unchanged and all(
            item.get("body") is True
            and item.get("overflow") is False
            and item.get("rows", 0) > 0
            and item.get("bandsOrdered") is True
            and item.get("panelsSeparated") is True
            and item.get("tableContained") is True
            and item.get("browserErrors") == []
            and item.get("screenshotBytes", 0) > 20_000
            for item in viewport_results
        )
        return {
            "schema_version": REPORT_SCHEMA,
            "passed": passed,
            "overlay_without_full_get": full_gets_unchanged,
            "before": before,
            "after": after,
            "viewports": viewport_results,
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


def _viewport(base: str, width: int, height: int) -> dict[str, Any]:
    _request_json(
        f"{base}/window/rect",
        method="POST",
        payload={"width": width, "height": height, "x": 0, "y": 0},
    )
    time.sleep(0.1)
    layout = _execute(
        base,
        "const header=document.querySelector('.v2-header').getBoundingClientRect();"
        "const status=document.querySelector('.status-band').getBoundingClientRect();"
        "const summary=document.querySelector('.summary-band').getBoundingClientRect();"
        "const controls=document.querySelector('.control-band').getBoundingClientRect();"
        "const table=document.querySelector('.table-region').getBoundingClientRect();"
        "const detail=document.querySelector('.detail-region').getBoundingClientRect();"
        "return {"
        "body:Boolean(document.body&&document.body.getBoundingClientRect().height>0),"
        "overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,"
        "rows:document.querySelectorAll('#decisionRows tr[data-code]').length,"
        "bandsOrdered:header.bottom<=status.top&&status.bottom<=summary.top"
        "&&summary.bottom<=controls.top&&controls.bottom<=table.top,"
        "panelsSeparated:table.right<=detail.left,"
        "tableContained:table.bottom<=document.querySelector('footer').getBoundingClientRect().top,"
        "browserErrors:window.TomorrowV2Diagnostics.errors"
        "};",
    )
    screenshot = _request_json(f"{base}/screenshot")
    encoded = screenshot.get("value")
    screenshot_bytes = len(encoded) * 3 // 4 if isinstance(encoded, str) else 0
    return {
        "requested": [width, height],
        **(layout if isinstance(layout, dict) else {}),
        "screenshotBytes": screenshot_bytes,
    }


if __name__ == "__main__":
    raise SystemExit(main())
