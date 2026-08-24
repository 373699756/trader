from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.application.decision_core import UnifiedDecisionIndex  # noqa: E402
from trader.application.decision_queries import UnifiedDecisionQueries  # noqa: E402
from trader.application.decision_stream import UnifiedDecisionEventStream  # noqa: E402
from trader.application.ports.decision_records import CommittedDecisionRecord  # noqa: E402
from trader.domain.recommendation.decision_identity import (  # noqa: E402
    DecisionItem,
    DecisionQuote,
    LongProjection,
    LongProjectionItem,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy  # noqa: E402
from trader.web import create_app  # noqa: E402
from trader.web.route_services import UnifiedWebServices  # noqa: E402
from trader.web.static_assets import WEB_ASSET_REVISION  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "v2-desktop-browser-v1"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 8, 13, 12, 30, tzinfo=_SHANGHAI)


class _BrowserClock:
    def now(self) -> datetime:
        return _NOW


class _BrowserHistory:
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        del strategy, trade_date
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        del strategy, limit
        return ()


class _ObservationResult(TypedDict):
    strategy: str
    visible: bool
    rows: int
    count: str
    quote_complete: bool
    quote_fields: list[str]
    codes: list[str]
    final_scores: list[float]
    ranked_high_first: bool


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
    server = make_server("127.0.0.1", app_port, create_app(services=_browser_services()), threaded=True)
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
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=today]").click(); return true;')
        _wait(lambda: _execute(base, 'return document.querySelector("#funnelStatus").textContent;') == "360 → 65 → 0")
        not_ready_summary = _execute(
            base,
            """
            return {
              age: document.querySelector('#quoteAge').textContent,
              source: document.querySelector('#quoteSource').textContent,
              coverage: document.querySelector('#quoteCoverageStatus').textContent,
              coverageMeta: document.querySelector('#quoteCoverageMeta').textContent,
              funnel: document.querySelector('#funnelStatus').textContent,
              funnelMeta: document.querySelector('#funnelMeta').textContent,
              budgetMeta: document.querySelector('#budgetMeta').textContent,
              freeze: document.querySelector('#headerFreeze').textContent,
            };
            """,
        )
        observations: list[_ObservationResult] = []
        expected_top_codes = {"tomorrow": "600009", "d25": "600010"}
        for strategy in ("tomorrow", "d25"):
            _execute(base, f'document.querySelector(".strategy-tab[data-strategy={strategy}]").click(); return true;')
            _wait(
                lambda: (
                    bool(_execute(base, 'return !document.querySelector("#observationPool").hidden;'))
                    and _integer(
                        _execute(base, 'return document.querySelectorAll("#observationBody tr[data-code]").length;')
                    )
                    > 0
                )
            )
            ranking = _execute(
                base,
                """
                const rows = Array.from(document.querySelectorAll('#observationBody tr[data-code]'));
                return {
                  codes: rows.map((row) => row.dataset.code),
                  finalScores: rows.map((row) => Number(row.querySelectorAll('.score-stack b')[3].textContent)),
                };
                """,
            )
            if not isinstance(ranking, dict):
                raise RuntimeError("browser observation ranking must be an object")
            codes = [str(code) for code in ranking.get("codes", [])]
            final_scores = [float(score) for score in ranking.get("finalScores", [])]
            observations.append(
                {
                    "strategy": strategy,
                    "visible": not bool(_execute(base, 'return document.querySelector("#observationPool").hidden;')),
                    "rows": _integer(
                        _execute(base, 'return document.querySelectorAll("#observationBody tr[data-code]").length;')
                    ),
                    "count": str(_execute(base, 'return document.querySelector("#funnelMeta").textContent;')),
                    "quote_complete": bool(
                        _execute(
                            base,
                            """
                            const cells = Array.from(document.querySelector('#observationBody tr[data-code]').cells)
                              .slice(2, 6).map((cell) => cell.textContent.trim());
                            return cells.length === 4
                              && cells[0] !== '-'
                              && cells[1] !== '-'
                              && !cells[2].includes('换手 -')
                              && cells[3] !== '-';
                            """,
                        )
                    ),
                    "quote_fields": list(
                        _execute(
                            base,
                            """
                            return Array.from(document.querySelector('#observationBody tr[data-code]').cells)
                              .slice(2, 6).map((cell) => cell.textContent.trim());
                            """,
                        )
                    ),
                    "codes": codes,
                    "final_scores": final_scores,
                    "ranked_high_first": (
                        codes[:1] == [expected_top_codes[strategy]]
                        and len(final_scores) == 2
                        and final_scores[0] > final_scores[1]
                    ),
                }
            )
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=long]").click(); return true;')
        _wait(
            lambda: _integer(_execute(base, 'return document.querySelectorAll("#tableBody tr[data-code]").length;')) > 0
        )
        long_quote_fields = _execute(
            base,
            """
            const row = document.querySelector('#tableBody tr[data-code="688127"]');
            const values = row ? Array.from(row.querySelectorAll('td')).map((cell) => cell.textContent.trim()) : [];
            return { code: row && row.dataset.code, values, complete: values.length > 0 && !values.includes('-') };
            """,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        _set_viewport(base, 1440, 900)
        _execute(base, 'document.querySelector("#errorDetailsButton").click(); return true;')
        _wait(
            lambda: bool(_execute(base, 'return document.querySelector("#errorDrawer").classList.contains("is-open");'))
        )
        _execute(base, 'document.querySelector("#errorDrawerContent button[data-copy-code]").click(); return true;')
        _wait(
            lambda: (
                _execute(
                    base,
                    'return document.querySelector("#errorDrawerContent button[data-copy-code]").textContent;',
                )
                != "复制代码"
            )
        )
        error_details = {
            "visible": bool(
                _execute(base, 'return document.querySelector("#errorDrawer").classList.contains("is-open");')
            ),
            "rows": _integer(
                _execute(base, 'return document.querySelectorAll("#errorDrawerContent .error-detail-item").length;')
            ),
            "raw_code_hidden_from_header": not bool(
                _execute(base, 'return document.querySelector("#lastError").textContent.includes("refresh:");')
            ),
            "copy_status": str(
                _execute(
                    base, 'return document.querySelector("#errorDrawerContent button[data-copy-code]").textContent;'
                )
            ),
        }
        detail_screenshot = _request_json(f"{base}/screenshot")["value"]
        (output_dir / "desktop-error-details-1440x900.png").write_bytes(base64.b64decode(str(detail_screenshot)))
        _execute(base, 'document.querySelector("#errorDrawerClose").click(); return true;')
        viewports = [_viewport(base, output_dir, width, height) for width, height in VIEWPORTS]
        scripts = _execute(base, "return Array.from(document.scripts).map((item) => item.src);")
        expected = f"/static/dashboard.js?rev={WEB_ASSET_REVISION}"
        passed = (
            isinstance(scripts, list)
            and any(expected in str(script) for script in scripts)
            and all(
                item["visible"]
                and item["rows"] == 2
                and "观察 2" in item["count"]
                and item["quote_complete"]
                and item["ranked_high_first"]
                for item in observations
            )
            and error_details["visible"] is True
            and error_details["rows"] == 2
            and error_details["raw_code_hidden_from_header"] is True
            and error_details["copy_status"] in {"已复制", "已选中，请复制"}
            and isinstance(long_quote_fields, dict)
            and long_quote_fields.get("complete") is True
            and isinstance(not_ready_summary, dict)
            and bool(re.fullmatch(r"(?:\d+h )?(?:\d+m )?\d+s", str(not_ready_summary.get("age"))))
            and not_ready_summary.get("source") == "腾讯行情"
            and not_ready_summary.get("coverage") == "352 / 360"
            and not_ready_summary.get("coverageMeta") == "行情缺失 8 · 身份缺失 286"
            and not_ready_summary.get("funnel") == "360 → 65 → 0"
            and not_ready_summary.get("funnelMeta") == "过滤 216 · 观察草稿 2 · 最高 74.25"
            and "上限 168" in str(not_ready_summary.get("budgetMeta"))
            and not_ready_summary.get("freeze") == "未就绪"
            and all(_viewport_passed(viewport) for viewport in viewports)
        )
        return {
            "schema_version": REPORT_SCHEMA,
            "passed": passed,
            "browser": "firefox-headless",
            "observations": observations,
            "not_ready_summary": not_ready_summary,
            "error_details": error_details,
            "long_quote_fields": long_quote_fields,
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


def _browser_services() -> UnifiedWebServices:
    index = UnifiedDecisionIndex()
    for strategy, codes in (
        (Strategy.TOMORROW, ("600009", "600001")),
        (Strategy.D25, ("600010", "600002")),
    ):
        items = tuple(
            _observation_item(code, rank=rank, final_score=score)
            for rank, (code, score) in enumerate(zip(codes, (74.0, 72.0), strict=True), start=1)
        )
        decision = ScoredDecision(
            strategy,
            _NOW.date(),
            1,
            _NOW.replace(hour=11, minute=15),
            "local",
            None,
            (("market", "market:browser"),),
            "config:browser",
            "strategy:browser",
            "fusion:browser",
            items,
            (),
        )
        if not index.publish(decision, expected_version=None).accepted:
            raise RuntimeError("browser observation fixture publication failed")
    long_projection = LongProjection(
        _NOW.date(),
        1,
        _NOW,
        (("quotes", "quotes:browser"),),
        (
            LongProjectionItem(
                "688127",
                "chokepoint:optics",
                "quote:688127",
                name="蓝特光学",
                industry="先进光刻/精密光学",
                price=66.02,
                pct_change=0.49,
                amount=203_037_318.0,
                turnover_rate=0.76,
                market_cap=26_797_000_000.0,
                source="tencent",
                source_time=_NOW.replace(second=0),
                quote_status="live",
            ),
        ),
    )
    if not index.publish(long_projection, expected_version=None).accepted:
        raise RuntimeError("browser long fixture publication failed")
    return UnifiedWebServices(
        UnifiedDecisionQueries(index, _BrowserHistory(), _BrowserClock()),
        UnifiedDecisionEventStream(),
        lambda: {
            "status": "running",
            "runtime_started": True,
            "phase": "midday",
            "deepseek_budget": {"used": 0, "remaining": 168, "planned_limit": 71},
            "scheduler": {
                "input_quality": {
                    "today": {
                        "status": "not_ready",
                        "supply_funnel": {
                            "requested_candidates": 360,
                            "full_scored": 65,
                            "filter_reject": 216,
                            "selected_executable": 0,
                            "selected_observe": 2,
                        },
                        "summary": {
                            "trade_date": _NOW.date().isoformat(),
                            "quote_total_count": 360,
                            "quote_covered_count": 352,
                            "quote_missing_count": 8,
                            "security_identity_missing_count": 286,
                            "latest_quote_source": "tencent",
                            "latest_quote_source_time": _NOW.replace(hour=10, minute=0).isoformat(),
                            "highest_final_score": 74.25,
                        },
                    }
                }
            },
            "health": {"level": "degraded", "issue_count": 2},
            "recent_errors": [
                {
                    "code": "refresh:source_unavailable",
                    "severity": "degraded",
                    "strategy": "tomorrow",
                    "stage": "refresh",
                    "occurred_at": _NOW.replace(hour=12, minute=20).isoformat(),
                    "last_occurred_at": _NOW.replace(hour=12, minute=24).isoformat(),
                    "count": 2,
                    "recovery_status": "active",
                    "resolved_at": None,
                },
                {
                    "code": "review:review_unavailable",
                    "severity": "degraded",
                    "strategy": "d25",
                    "stage": "review",
                    "occurred_at": _NOW.replace(hour=12, minute=18).isoformat(),
                    "last_occurred_at": _NOW.replace(hour=12, minute=18).isoformat(),
                    "count": 1,
                    "recovery_status": "recovered",
                    "resolved_at": _NOW.replace(hour=12, minute=22).isoformat(),
                },
            ],
        },
    )


def _observation_item(code: str, *, rank: int, final_score: float) -> DecisionItem:
    return DecisionItem(
        code,
        RecommendationAction.OBSERVE,
        True,
        rank,
        final_score,
        final_score,
        final_score,
        (("local_score", final_score),),
        (),
        "observation_band",
        quote=DecisionQuote(
            code,
            10.25,
            2.5,
            1_000_000_000.0,
            0.8,
            300_000_000_000.0,
            "fixture",
            _NOW.replace(hour=11, minute=15),
            f"quote:{code}",
        ),
    )


def _viewport(base: str, output_dir: Path, width: int, height: int) -> dict[str, object]:
    _set_viewport(base, width, height)
    time.sleep(0.2)
    result = _execute(
        base,
        r"""
        const header = document.querySelector('.app-header').getBoundingClientRect();
        const messages = Array.from(document.querySelectorAll('.runtime-message')).map((item) => item.getBoundingClientRect());
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
          messageColumns: messages.length,
          messageEqualHeight: messages.length === 2 && Math.abs(messages[0].height - messages[1].height) < 1,
          summaryItems: document.querySelectorAll('.summary-band > .summary-item').length,
          quoteAge: document.querySelector('#quoteAge').textContent,
          quoteAgeHms: /^\d+h \d+m \d+s$/.test(document.querySelector('#quoteAge').textContent),
          quoteCoverage: document.querySelector('#quoteCoverageStatus').textContent,
          quoteCoverageMeta: document.querySelector('#quoteCoverageMeta').textContent,
          longWatchlistSize: window.TraderLongWatchlistData.items.length,
          snapshotDate: document.querySelector('#snapshotDate').textContent,
          healthBadge: document.querySelector('#healthBadge').textContent,
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
    if not isinstance(result, dict):
        raise RuntimeError("browser viewport result must be an object")
    return {"requested": [width, height], "screenshot": screenshot_name, **result}


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
    watchlist_size = result.get("longWatchlistSize")
    expected_coverage = f"1 / {watchlist_size}"
    expected_missing = f"行情缺失 {int(watchlist_size) - 1} · 身份缺失 0" if isinstance(watchlist_size, int) else ""
    return bool(
        result.get("body")
        and result.get("actual") == result.get("requested")
        and not result.get("overflow")
        and result.get("ordered")
        and result.get("longVisible")
        and result.get("noLongOverlap")
        and result.get("messageColumns") == 2
        and result.get("messageEqualHeight")
        and result.get("summaryItems") == 5
        and result.get("quoteAgeHms")
        and result.get("quoteCoverage") == expected_coverage
        and result.get("quoteCoverageMeta") == expected_missing
        and result.get("snapshotDate") == _NOW.date().isoformat()
        and "2026/" not in str(result.get("notice"))
        and "12:30:00" in str(result.get("notice"))
        and result.get("healthBadge") == "降级 · 2项"
        and result.get("rows")
        and result.get("scopes") == 3
        and result.get("browserErrors") == []
    )


def _execute(base: str, script: str) -> object:
    response = _request_json(f"{base}/execute/sync", method="POST", payload={"script": script, "args": []})
    return response.get("value")


def _integer(value: object) -> int:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RuntimeError("browser result must be numeric")
    return int(value)


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
