from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

import websocket
from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.application.decisions.decision_core import UnifiedDecisionIndex  # noqa: E402
from trader.application.decisions.decision_drafts import UnifiedDecisionDraftIndex  # noqa: E402
from trader.application.decisions.decision_queries import UnifiedDecisionQueries  # noqa: E402
from trader.application.decisions.decision_stream import UnifiedDecisionEventStream  # noqa: E402
from trader.application.ports.decision_records import CommittedDecisionRecord  # noqa: E402
from trader.domain.recommendation.decision_identity import (  # noqa: E402
    DecisionItem,
    DecisionQuote,
    LongProjection,
    LongProjectionItem,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy  # noqa: E402
from trader.web import create_app  # noqa: E402
from trader.web.api.route_services import UnifiedWebServices  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "desktop-browser"
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
    app_port = _free_port()
    services, publish_empty_draft = _browser_services()
    server = make_server("127.0.0.1", app_port, create_app(services=services), threaded=True)
    driver_port = _free_port()
    server_thread = threading.Thread(target=server.serve_forever, name="browser-fixture", daemon=True)
    server_thread.start()
    driver: subprocess.Popen[str] | None = None
    chrome: _ChromeSession | None = None
    session_id: str | None = None
    try:
        if driver_binary is not None and shutil.which("firefox") is not None:
            driver = subprocess.Popen(
                [driver_binary, "--host", "127.0.0.1", "--port", str(driver_port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
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
            base: str | _ChromeSession = f"http://127.0.0.1:{driver_port}/session/{session_id}"
            browser_name = "firefox-headless"
        else:
            chrome_binary = shutil.which("google-chrome") or shutil.which("chromium")
            if chrome_binary is None:
                raise RuntimeError("Firefox/geckodriver or Google Chrome is required for the desktop gate")
            chrome = _ChromeSession(chrome_binary, driver_port)
            base = chrome
            browser_name = "chrome-headless"
        _navigate(base, f"http://127.0.0.1:{app_port}/")
        _wait(lambda: bool(_execute(base, "return Boolean(window.TraderDashboardDiagnostics);")), "dashboard readiness")
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=tomorrow]").click(); return true;')
        _wait(
            lambda: _execute(base, 'return document.querySelector("#funnelStatus").textContent;') == "360 → 采集中 → 0",
            "collecting funnel",
        )
        not_ready_summary = _execute(
            base,
            """
            return {
              age: document.querySelector('#quoteAge').textContent,
              source: document.querySelector('#quoteSource').textContent,
              inputQuality: document.querySelector('#inputQualityStatus').textContent,
              inputQualityMeta: document.querySelector('#inputQualityMeta').textContent,
              funnel: document.querySelector('#funnelStatus').textContent,
              funnelMeta: document.querySelector('#funnelMeta').textContent,
              budgetMeta: document.querySelector('#budgetMeta').textContent,
              publicationStatus: document.querySelector('#publicationStatus').textContent,
              publicationMeta: document.querySelector('#publicationMeta').textContent,
            };
            """,
        )
        _wait(
            lambda: (
                _integer(_execute(base, 'return document.querySelectorAll("#observationBody tr[data-code]").length;'))
                == 2
            ),
            "tomorrow observation draft",
        )
        _wait(
            lambda: _execute(base, 'return document.querySelector("#funnelStatus").textContent;') == "360 → 56 → 0",
            "quality funnel",
        )
        quality_summary = _execute(
            base,
            """
            return {
              inputQuality: document.querySelector('#inputQualityStatus').textContent,
              inputQualityMeta: document.querySelector('#inputQualityMeta').textContent,
              funnel: document.querySelector('#funnelStatus').textContent,
              funnelMeta: document.querySelector('#funnelMeta').textContent,
              source: document.querySelector('#quoteSource').textContent,
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
                ),
                f"{strategy} observation rows",
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
            raw_quote_fields = _execute(
                base,
                """
                return Array.from(document.querySelector('#observationBody tr[data-code]').cells)
                  .slice(2, 6).map((cell) => cell.textContent.trim());
                """,
            )
            if not isinstance(raw_quote_fields, list):
                raise RuntimeError("browser observation quote fields must be a list")
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
                    "quote_fields": [str(value) for value in raw_quote_fields],
                    "codes": codes,
                    "final_scores": final_scores,
                    "ranked_high_first": (
                        codes[:1] == [expected_top_codes[strategy]]
                        and len(final_scores) == 2
                        and final_scores[0] > final_scores[1]
                    ),
                }
            )
        publish_empty_draft()
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=tomorrow]").click(); return true;')
        _wait(
            lambda: str(_execute(base, 'return document.querySelector("#tableBody").textContent.trim();')).startswith(
                "评分已完成｜最高分 74.25"
            ),
            "scored-empty recommendation explanation",
        )
        empty_observation = {
            "visible": not bool(_execute(base, 'return document.querySelector("#observationPool").hidden;')),
            "rows": _integer(
                _execute(base, 'return document.querySelectorAll("#observationBody tr[data-code]").length;')
            ),
            "message": str(_execute(base, 'return document.querySelector("#observationBody").textContent.trim();')),
            "summary": str(_execute(base, 'return document.querySelector("#funnelMeta").textContent;')),
            "recommendation_message": str(
                _execute(base, 'return document.querySelector("#tableBody").textContent.trim();')
            ),
        }
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=long]").click(); return true;')
        _wait(
            lambda: (
                _integer(_execute(base, 'return document.querySelectorAll("#tableBody tr[data-code]").length;')) > 0
            ),
            "long table rows",
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
            lambda: bool(
                _execute(base, 'return document.querySelector("#errorDrawer").classList.contains("is-open");')
            ),
            "error drawer open",
        )
        _execute(base, 'document.querySelector("#errorDrawerContent button[data-copy-code]").click(); return true;')
        _wait(
            lambda: (
                _execute(
                    base,
                    'return document.querySelector("#errorDrawerContent button[data-copy-code]").textContent;',
                )
                != "复制代码"
            ),
            "error detail copy",
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
        detail_screenshot = _screenshot(base)
        (output_dir / "desktop-error-details-1440x900.png").write_bytes(base64.b64decode(str(detail_screenshot)))
        _execute(base, 'document.querySelector("#errorDrawerClose").click(); return true;')
        viewports = [_viewport(base, output_dir, width, height) for width, height in VIEWPORTS]
        scripts = _execute(base, "return Array.from(document.scripts).map((item) => item.src);")
        expected = "/static/dashboard.js"
        passed = (
            isinstance(scripts, list)
            and any(expected in str(script) for script in scripts)
            and all(
                item["visible"]
                and item["rows"] == 2
                and "观察草稿 2" in item["count"]
                and item["quote_complete"]
                and item["ranked_high_first"]
                for item in observations
            )
            and empty_observation
            == {
                "visible": True,
                "rows": 0,
                "message": (
                    "评分已完成｜最高分 74.25，距离正式线 3.75；达到观察线 2只、正式线 0只；"
                    "主要原因：评分未达到执行门槛（54只）、风险事实触发限制（2只）、"
                    "公司风险历史暂不可核验（1只）"
                ),
                "summary": "过滤 216 · 观察 0 · 最高 -",
                "recommendation_message": (
                    "评分已完成｜最高分 74.25，距离正式线 3.75；达到观察线 2只、正式线 0只；"
                    "主要原因：评分未达到执行门槛（54只）、风险事实触发限制（2只）、"
                    "公司风险历史暂不可核验（1只）"
                ),
            }
            and error_details["visible"] is True
            and error_details["rows"] == 2
            and error_details["raw_code_hidden_from_header"] is True
            and error_details["copy_status"] in {"已复制", "已选中，请复制"}
            and isinstance(long_quote_fields, dict)
            and long_quote_fields.get("complete") is True
            and isinstance(not_ready_summary, dict)
            and bool(re.fullmatch(r"(?:\d+时 )?(?:\d+分 )?\d+秒", str(not_ready_summary.get("age"))))
            and not_ready_summary.get("source") == "腾讯行情"
            and not_ready_summary.get("inputQuality") == "评分输入准备中"
            and not_ready_summary.get("inputQualityMeta") == "行情 360 / 360 · 基础资料与历史待计算"
            and not_ready_summary.get("funnel") == "360 → 采集中 → 0"
            and not_ready_summary.get("funnelMeta") == "过滤 待计算 · 观察草稿 正在生成 · 最高 —"
            and "上限 168" in str(not_ready_summary.get("budgetMeta"))
            and not_ready_summary.get("publicationStatus") == "采集中"
            and not_ready_summary.get("publicationMeta") == "等待本轮正式结果"
            and quality_summary
            == {
                "inputQuality": "可评分 56 / 候选 360",
                "inputQualityMeta": "历史 78 / 360 · 21.7% · 证券资料 120 / 360",
                "funnel": "360 → 56 → 0",
                "funnelMeta": "过滤 216 · 观察草稿 2 · 最高 74.25",
                "source": "腾讯行情",
            }
            and all(_viewport_passed(viewport) for viewport in viewports)
        )
        return {
            "schema_version": REPORT_SCHEMA,
            "passed": passed,
            "browser": browser_name,
            "observations": observations,
            "empty_observation": empty_observation,
            "not_ready_summary": not_ready_summary,
            "quality_summary": quality_summary,
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
        if driver is not None:
            driver.terminate()
            try:
                driver.wait(timeout=5)
            except subprocess.TimeoutExpired:
                driver.kill()
                driver.wait(timeout=5)
        if chrome is not None:
            chrome.close()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _browser_services() -> tuple[UnifiedWebServices, Callable[[], None]]:
    index = UnifiedDecisionIndex()
    drafts = UnifiedDecisionDraftIndex()
    decisions: dict[Strategy, ScoredDecision] = {}
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
        decisions[strategy] = decision
    if not drafts.publish(decisions[Strategy.D25]).accepted:
        raise RuntimeError("browser d25 draft publication failed")
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
    status_calls = 0
    empty_draft_published = False

    def status_provider() -> dict[str, object]:
        nonlocal status_calls
        status_calls += 1
        if status_calls == 2:
            result = drafts.publish(decisions[Strategy.TOMORROW])
            if not result.accepted:
                raise RuntimeError(f"browser tomorrow draft publication failed: {result.reason}")
        input_quality = _browser_input_quality(empty=empty_draft_published) if status_calls >= 2 else {}
        return {
            "status": "running",
            "runtime_started": True,
            "phase": "midday",
            "deepseek_budget": {"used": 0, "remaining": 168, "planned_limit": 71},
            "market_data": {
                "active_source": "eastmoney",
                "candidate_quote_latest_source": "tencent",
                "candidate_quote_cache_entries": 360,
                "security_master": {
                    "provider": "free_market+production_calendar",
                    "tushare_required": False,
                },
                "candidate_quote_age": {
                    "sample_count": 360,
                    "latest_source_time": _NOW.replace(hour=10, minute=0).isoformat(),
                },
            },
            "scheduler": {
                "input_quality": input_quality,
                "lanes": [
                    {"strategy": "tomorrow", "running": True, "pending": True},
                    {"strategy": "d25", "running": True, "pending": True},
                ],
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
        }

    def publish_empty_draft() -> None:
        nonlocal empty_draft_published
        empty = ScoredDecision(
            Strategy.TOMORROW,
            _NOW.date(),
            3,
            _NOW.replace(hour=12, minute=31),
            "local",
            None,
            (("market", "market:browser-empty"),),
            "config:browser",
            "strategy:browser",
            "fusion:browser",
            tuple(
                replace(
                    _observation_item(code, rank=rank, final_score=score),
                    selected=False,
                    rank=0,
                    action=RecommendationAction.UNAVAILABLE,
                    reason="risk_veto",
                )
                for rank, (code, score) in enumerate(
                    zip(("600009", "600001"), (74.25, 72.0), strict=True),
                    start=1,
                )
            ),
            (),
            population_count=218,
            rejected_count=216,
            selection_diagnostics=SelectionDiagnostics(
                74.25,
                78.0,
                70.0,
                6,
                6,
                0,
                0,
                0,
                "risk_or_execution_blocked",
            ),
        )
        result = index.publish(empty, expected_version=None)
        if not result.accepted:
            raise RuntimeError(f"browser empty decision publication failed: {result.reason}")
        empty_draft_published = True

    return (
        UnifiedWebServices(
            UnifiedDecisionQueries(index, drafts, _BrowserHistory(), _BrowserClock()),
            UnifiedDecisionEventStream(),
            status_provider,
        ),
        publish_empty_draft,
    )


def _browser_input_quality(*, empty: bool = False) -> dict[str, object]:
    status = {
        "status": "not_ready",
        "candidate_optional_reason_counts": {
            "missing_listing_date": 221,
            "missing_listing_age_sessions": 65,
        },
        "supply_funnel": {
            "requested_candidates": 360,
            "security_master": 120,
            "history": 78,
            "full_scored": 56,
            "filter_reject": 216,
            "observation_threshold_met_count": 2,
            "executable_threshold_met_count": 0,
            "selected_executable": 0,
            "selected_observe": 0 if empty else 2,
        },
        "supply_reason_counts": {
            "below_score_threshold": 54,
            "risk_veto": 2,
            "corporate_risk_history_unavailable": 1,
            "stale_quote": 1,
        },
        "primary_blocker": "security_master_coverage_incomplete",
        "summary": {
            "trade_date": _NOW.date().isoformat(),
            "quote_total_count": 360,
            "quote_covered_count": 360,
            "quote_missing_count": 0,
            "security_identity_missing_count": 240,
            "latest_quote_source": "tencent",
            "latest_quote_source_time": _NOW.replace(hour=10, minute=0).isoformat(),
            "highest_final_score": 74.25,
        },
    }
    return {strategy: status for strategy in ("tomorrow", "d25")}


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


def _viewport(base: str | _ChromeSession, output_dir: Path, width: int, height: int) -> dict[str, object]:
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
          quoteAgeHms: /^\d+(?:时 \d+分 )?\d+秒$/.test(document.querySelector('#quoteAge').textContent),
          inputQuality: document.querySelector('#inputQualityStatus').textContent,
          inputQualityMeta: document.querySelector('#inputQualityMeta').textContent,
          longWatchlistSize: window.TraderLongWatchlistData.items.length,
          publicationStatus: document.querySelector('#publicationStatus').textContent,
          publicationMeta: document.querySelector('#publicationMeta').textContent,
          topScoresStatus: document.querySelector('#topScoresStatus').textContent,
          healthBadge: document.querySelector('#healthBadge').textContent,
          rows: document.querySelectorAll('#tableBody tr[data-code]').length,
          scopes: document.querySelectorAll('#longScopeTabs button[data-scope]').length,
          browserErrors: window.TraderDashboardDiagnostics.snapshot().browserErrors,
        };
        """,
    )
    screenshot = _screenshot(base)
    screenshot_name = f"desktop-{width}x{height}.png"
    (output_dir / screenshot_name).write_bytes(base64.b64decode(str(screenshot)))
    if not isinstance(result, dict):
        raise RuntimeError("browser viewport result must be an object")
    return {"requested": [width, height], "screenshot": screenshot_name, **result}


def _set_viewport(base: str | _ChromeSession, width: int, height: int) -> None:
    if isinstance(base, _ChromeSession):
        base.set_viewport(width, height)
        return
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
        and result.get("messageColumns") == 2
        and result.get("messageEqualHeight")
        and result.get("summaryItems") == 4
        and result.get("quoteAgeHms")
        and result.get("inputQuality") == "不适用"
        and result.get("inputQualityMeta") == "长期固定观察池不评分"
        and result.get("publicationStatus") == "不适用"
        and result.get("publicationMeta") == "长期固定观察池，不评分、不冻结"
        and result.get("topScoresStatus") == "暂无评分数据"
        and result.get("healthBadge") == "降级 · 2项"
        and result.get("rows")
        and result.get("scopes") == 3
        and result.get("browserErrors") == []
    )


def _navigate(base: str | _ChromeSession, url: str) -> None:
    if isinstance(base, _ChromeSession):
        base.navigate(url)
        return
    _request_json(f"{base}/url", method="POST", payload={"url": url})


def _screenshot(base: str | _ChromeSession) -> object:
    if isinstance(base, _ChromeSession):
        return base.screenshot()
    return _request_json(f"{base}/screenshot")["value"]


def _execute(base: str | _ChromeSession, script: str) -> object:
    if isinstance(base, _ChromeSession):
        return base.execute(script)
    response = _request_json(f"{base}/execute/sync", method="POST", payload={"script": script, "args": []})
    return response.get("value")


class _ChromeSession:
    def __init__(self, binary: str, port: int) -> None:
        self._next_id = 0
        self._profile = tempfile.TemporaryDirectory(prefix="trader-desktop-chrome-")
        self._process = subprocess.Popen(
            [
                binary,
                "--headless=new",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self._profile.name}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            debugger_url = self._wait_debugger_url(port)
            self._socket = websocket.create_connection(debugger_url, timeout=15)
            self._command("Runtime.enable")
            self._command("Page.enable")
        except Exception:
            self.close()
            raise

    def _wait_debugger_url(self, port: int) -> str:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                error = self._process.stderr.read() if self._process.stderr is not None else ""
                raise RuntimeError(f"Chrome exited before readiness: {error[:300]}")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as response:
                    targets = json.loads(response.read().decode())
                if isinstance(targets, list):
                    page = next((target for target in targets if target.get("type") == "page"), None)
                    if isinstance(page, dict) and isinstance(page.get("webSocketDebuggerUrl"), str):
                        return page["webSocketDebuggerUrl"]
            except (OSError, json.JSONDecodeError, urllib.error.URLError):
                pass
            time.sleep(0.05)
        raise RuntimeError("Chrome DevTools endpoint did not become ready")

    def _command(self, method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
        self._next_id += 1
        command_id = self._next_id
        self._socket.send(json.dumps({"id": command_id, "method": method, "params": params or {}}))
        while True:
            response = json.loads(self._socket.recv())
            if not isinstance(response, dict) or response.get("id") != command_id:
                continue
            if "error" in response:
                raise RuntimeError(f"Chrome DevTools command failed: {response['error']}")
            result = response.get("result", {})
            return result if isinstance(result, dict) else {}

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
            raise RuntimeError(f"Chrome JavaScript evaluation failed: {response['exceptionDetails']}")
        result = response.get("result", {})
        return result.get("value") if isinstance(result, dict) else None

    def set_viewport(self, width: int, height: int) -> None:
        self._command(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def screenshot(self) -> object:
        return self._command("Page.captureScreenshot", {"format": "png", "fromSurface": True}).get("data")

    def close(self) -> None:
        socket = getattr(self, "_socket", None)
        if socket is not None:
            socket.close()
        process = getattr(self, "_process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        profile = getattr(self, "_profile", None)
        if profile is not None:
            _cleanup_browser_profile(profile)


def _cleanup_browser_profile(
    profile: tempfile.TemporaryDirectory[str],
    *,
    attempts: int = 20,
    delay_seconds: float = 0.05,
) -> None:
    for attempt in range(attempts):
        try:
            profile.cleanup()
            return
        except OSError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay_seconds)


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


def _wait(condition: Any, description: str = "condition") -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.05)
    raise RuntimeError(f"browser condition timed out: {description}")


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
