from __future__ import annotations

import argparse
import base64
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import websocket
from werkzeug.serving import make_server

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.bootstrap import OpenExternalHttp  # noqa: E402
from trader.http_api.route_services import ProfiledWebServices, UnifiedWebServices  # noqa: E402
from trader.infra.http_contracts import HttpRequest, HttpStatusError  # noqa: E402
from trader.recommendation.application.pipeline.freeze_publish.draft_index import (  # noqa: E402
    UnifiedDecisionDraftIndex,
)
from trader.recommendation.application.pipeline.freeze_publish.event_stream import (  # noqa: E402
    UnifiedDecisionEventStream,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import (  # noqa: E402
    DecisionQuerySources,
    UnifiedDecisionQueries,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (  # noqa: E402
    UnifiedDecisionIndex,
)
from trader.recommendation.application.ports.decision_records import CommittedDecisionRecord  # noqa: E402
from trader.recommendation.domain.market.models import Board, ModelIndustryReference  # noqa: E402
from trader.recommendation.domain.publication.decision_identity import (  # noqa: E402
    DecisionItem,
    DecisionQuote,
    LongProjection,
    LongProjectionItem,
    ScoredDecision,
    SelectionDiagnostics,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy  # noqa: E402
from trader.web import CreateApp  # noqa: E402

VIEWPORTS = ((1280, 720), (1440, 900), (1920, 1080))
REPORT_SCHEMA = "desktop-browser"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_NOW = datetime(2026, 8, 13, 12, 30, tzinfo=_SHANGHAI)


class _BrowserClock:
    def now(self) -> datetime:
        return _NOW


class _BrowserIndustryClassifications:
    def model_industries(self, codes: Sequence[str]) -> dict[str, ModelIndustryReference]:
        if "600009" not in codes:
            return {}
        return {
            "600009": ModelIndustryReference(
                "G56航空运输业", "证监会行业分类", _NOW.date(), "fixture", "industry:browser"
            )
        }


class _BrowserHistory:
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        del strategy, trade_date
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        del strategy, limit
        return ()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--filter-counts-only", action="store_true", help="check filter disposition counts")
    args = parser.parse_args()
    try:
        report = _run(args.output.parent, filter_counts_only=args.filter_counts_only)
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


def _run(output_dir: Path, *, filter_counts_only: bool = False) -> dict[str, object]:
    driver_binary = shutil.which("geckodriver")
    app_port = _free_port()
    services, publish_empty_draft = _browser_services()
    server = make_server("127.0.0.1", app_port, CreateApp(services=services), threaded=True)
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
        if filter_counts_only:
            return _filter_counts(base, browser_name)
        output_dir.mkdir(parents=True, exist_ok=True)
        snapshots: list[dict[str, object]] = []
        for strategy in ("tomorrow", "d25"):
            _execute(base, f'document.querySelector(".strategy-tab[data-strategy={strategy}]").click(); return true;')
            _wait(
                lambda: (
                    _integer(_execute(base, 'return document.querySelectorAll("#tableBody tr[data-code]").length;'))
                    == 2
                ),
                f"{strategy} scored rows",
            )
            snapshot = _six_seat_snapshot(base)
            snapshots.append({"strategy": strategy, **snapshot})
        _execute(base, 'document.querySelector(".profile-tab[data-profile=v3]").click(); return true;')
        _wait(
            lambda: bool(
                _execute(
                    base,
                    """
                return ['status', 'decisions/d25/dates', 'decisions/d25/current'].every((path) =>
                  performance.getEntriesByType('resource').some((entry) =>
                    entry.name.includes('/api/' + path) && entry.name.includes('profile=v3')));
            """,
                )
            ),
            "v3 profile requests",
        )
        profile_switch = _execute(
            base,
            """
            return {selected: document.querySelector('.profile-tab[aria-selected=true]')?.dataset.profile,
              before_date: document.querySelector('.profile-control').getBoundingClientRect().right
                <= document.querySelector('.date-control').getBoundingClientRect().left,
              rows: document.querySelectorAll('#tableBody tr[data-code]').length};
        """,
        )
        _execute(base, 'document.querySelector(".profile-tab[data-profile=v2]").click(); return true;')
        _wait(
            lambda: bool(
                _execute(
                    base, "return Boolean(document.querySelector('.profile-tab[data-profile=v2][aria-selected=true]'));"
                )
            ),
            "v2 profile selection",
        )
        publish_empty_draft()
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=d25]").click(); return true;')
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=tomorrow]").click(); return true;')
        _wait(
            lambda: _execute(base, 'return document.querySelector("#tableBody .tier-tag")?.textContent;') == "回避",
            "avoided scored rows",
        )
        avoided = _six_seat_snapshot(base)
        observation_copy = _observation_copyability(base)
        viewports = [_viewport(base, output_dir, width, height) for width, height in VIEWPORTS]
        _execute(base, 'document.querySelector(".strategy-tab[data-strategy=long]").click(); return true;')
        _wait(
            lambda: bool(_execute(base, 'return Boolean(document.querySelector("#tableBody tr[data-code]"));')),
            "long rows",
        )
        long_state = _execute(
            base,
            """
            const row = document.querySelector('#tableBody tr[data-code]');
            row.click();
            const drawer = document.querySelector('#detailDrawer');
            const opened = drawer.classList.contains('is-open');
            document.querySelector('#drawerClose').click();
            return {rows: document.querySelectorAll('#tableBody tr[data-code]').length,
              status: document.querySelector('#sixSeatStatus').textContent,
              opened, closed: !drawer.classList.contains('is-open')};
            """,
        )
        passed = (
            all(
                item.get("headers", [])[:3] == ["排名", "状态", "股票"]
                and item.get("codes") == expected_codes
                and item.get("ranks") == [1, 2]
                and item.get("tiers") == ["关注", "关注"]
                and item.get("tier_colors_distinct") is True
                and item.get("cards") == 4
                and item.get("six_seat") == "2 / 6 只"
                and item.get("browser_errors") == []
                for item, (_, expected_codes) in zip(
                    snapshots, (("tomorrow", ["600009", "600001"]), ("d25", ["600010", "600002"])), strict=True
                )
            )
            and avoided.get("codes") == ["600009", "600001"]
            and avoided.get("ranks") == [1, 2]
            and avoided.get("tiers") == ["回避", "回避"]
            and avoided.get("six_seat_meta") == "推荐 0 · 关注 0 · 回避 2"
            and observation_copy.get("passed") is True
            and profile_switch == {"selected": "v3", "before_date": True, "rows": 2}
            and all(_viewport_passed(viewport) for viewport in viewports)
            and isinstance(long_state, dict)
            and long_state.get("rows", 0) > 0
            and long_state.get("status") == "不适用"
            and long_state.get("opened") is True
            and long_state.get("closed") is True
        )
        return {
            "schema_version": REPORT_SCHEMA,
            "passed": passed,
            "browser": browser_name,
            "snapshots": snapshots,
            "avoided": avoided,
            "observation_copy": observation_copy,
            "profile_switch": profile_switch,
            "long": long_state,
            "viewports": viewports,
            "external_network_calls": 0,
        }
    finally:
        if session_id is not None:
            try:
                _request_json(
                    f"http://127.0.0.1:{driver_port}/session/{session_id}",
                    method="DELETE",
                )
            except (OSError, RuntimeError):
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


def _filter_counts(base: str | _ChromeSession, browser_name: str) -> dict[str, object]:
    results = []
    for width, height in VIEWPORTS:
        _set_viewport(base, width, height)
        result = _execute(
            base,
            """
            const list = document.querySelector('#observationStageList');
            const drawer = document.querySelector('#observationDrawer');
            drawer.classList.add('is-open');
            const stages = ['data_source', 'static_market', 'static_standardize', 'static_filter',
              'dynamic_market', 'dynamic_standardize', 'dynamic_filter', 'candidate_pool',
              'quality_check', 'local_score', 'risk_review', 'score_merge', 'downside_action', 'final_selection'];
            window.TraderStatusView.renderObservationStages({observationStageList: list}, stages.map(stage =>
              ({stage, state: 'degraded', input_count: 4391, output_count: 129,
                rejected_count: 1079, pending_count: 3182, failed_count: 1, latency_ms: 1000})
            ), null, false, []);
            const row = list.querySelector('[data-stage-keys="dynamic_filter"]');
            const text = row.querySelector('span');
            const expected = '4391 → 129 · 淘汰 1079 · 待补充 3182 · 失败 1';
            return {text: text.textContent, passed: text.textContent.includes(expected)
              && text.scrollWidth <= text.clientWidth && row.getBoundingClientRect().width > 0};
        """,
        )
        results.append({"viewport": [width, height], **result})
    return {
        "schema_version": REPORT_SCHEMA,
        "browser": browser_name,
        "passed": all(result["passed"] for result in results),
        "filter_counts": results,
    }


def _six_seat_snapshot(base: str | _ChromeSession) -> dict[str, object]:
    result = _execute(
        base,
        """
        const rows = Array.from(document.querySelectorAll('#tableBody tr[data-code]'));
        const tierColors = ['recommend', 'watch', 'avoid'].map((tier) => {
          const tag = document.createElement('span');
          tag.className = 'tier-tag';
          tag.dataset.tier = tier;
          document.body.appendChild(tag);
          const color = getComputedStyle(tag).color;
          tag.remove();
          return color;
        });
        return {
          headers: Array.from(document.querySelectorAll('#tableHead th')).map((cell) => cell.textContent.trim()),
          codes: rows.map((row) => row.dataset.code),
          ranks: rows.map((row) => Number(row.cells[0].textContent)),
          tiers: rows.map((row) => row.cells[1].textContent.trim()),
          cards: document.querySelectorAll('.summary-band > .summary-item').length,
          six_seat: document.querySelector('#sixSeatStatus').textContent,
          six_seat_meta: document.querySelector('#sixSeatMeta').textContent,
          tier_colors_distinct: new Set(tierColors).size === 3,
          browser_errors: window.TraderDashboardDiagnostics.snapshot().browserErrors,
        };
        """,
    )
    if not isinstance(result, dict):
        raise RuntimeError("browser six-seat snapshot must be an object")
    return result


def _observation_copyability(base: str | _ChromeSession) -> dict[str, object]:
    result = _execute(
        base,
        """
        document.querySelector('#errorDetailsButton').click();
        const list = document.querySelector('#observationStageList');
        const stage = Array.from(list.querySelectorAll('.observation-stage'))
          .find((item) => item.querySelector('.observation-stage-detail')?.textContent.trim());
        if (!stage) return {passed: false, reason: 'no_stage_detail'};
        const button = stage.querySelector('button[data-stage-toggle]');
        const detail = stage.querySelector('.observation-stage-detail');
        button.click();
        const range = document.createRange();
        range.selectNodeContents(detail);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        const selected = selection.toString().trim();
        detail.dispatchEvent(new MouseEvent('click', {bubbles: true}));
        const stillExpanded = stage.classList.contains('is-expanded');
        window.TraderStatusView.renderObservationStages({observationStageList: list}, [], null, false, []);
        const preserved = stage.isConnected && selection.toString().trim() === selected;
        selection.removeAllRanges();
        document.querySelector('#observationDrawerClose').click();
        return {passed: button.tagName === 'BUTTON' && !button.contains(detail)
          && getComputedStyle(detail).userSelect === 'text'
          && selected.length > 0 && stillExpanded && preserved,
          selected_length: selected.length, still_expanded: stillExpanded, preserved};
        """,
    )
    if not isinstance(result, dict):
        raise RuntimeError("browser observation copyability result must be an object")
    return result


def _browser_services() -> tuple[ProfiledWebServices, Callable[[], None]]:
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
                    selection_rank=0,
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
        ProfiledWebServices(
            {
                profile: UnifiedWebServices(
                    UnifiedDecisionQueries(
                        index,
                        drafts,
                        _BrowserHistory(),
                        _BrowserClock(),
                        DecisionQuerySources(industry_classifications=_BrowserIndustryClassifications()),
                    ),
                    UnifiedDecisionEventStream(),
                    status_provider,
                    profile_id=profile,
                )
                for profile in ("v2", "v3")
            },
            "v2",
        ),
        publish_empty_draft,
    )


def _browser_input_quality(*, empty: bool = False) -> dict[str, object]:
    status = {
        "status": "not_ready",
        "population_count": 5291,
        "data_pending_count": 4291,
        "candidate_optional_reason_counts": {
            "missing_listing_date": 221,
            "missing_listing_age_sessions": 65,
        },
        "pipeline": {
            "current_stage": "concentration",
            "stages": [
                _browser_stage("input_readiness", 5291, 1000),
                _browser_stage("dynamic_filter", 1000, 500),
                _browser_stage("board_cross_section", 500, 500, filter_pass=56, filter_observe=2, filter_reject=216),
                _browser_stage("strategy_history", 500, 400, history_sessions=(61.0, 248.0)),
                _browser_stage("model_input", 400, 360, input_completeness=(0.82, 1.0)),
                _browser_stage("candidate_score", 360, 360, candidate_score=(49.0, 82.0), threshold=50.0),
                _browser_stage("board_limit", 360, 360),
                _browser_stage("candidate_refresh", 360, 360, quote_age_seconds=(1.0, 3.0)),
                _browser_stage("input_coverage", 360, None, candidate_features=360, security_master=120, history=78),
                _browser_stage("evidence_score", 360, 56, base_score=(42.0, 76.0)),
                _browser_stage("model_cost_gate", 56, 20, predicted_net_excess_pct=(-0.2, 0.8)),
                _browser_stage("local_score", 56, 56, local_score=(40.0, 74.25)),
                _browser_stage("deepseek_review", 20, 20, deepseek_score=(40.0, 76.0)),
                _browser_stage("fusion", 56, 56, final_score=(40.0, 74.25)),
                _browser_stage(
                    "action_gate",
                    56,
                    2,
                    observation_threshold_met=2,
                    executable_threshold_met=0,
                    action_executable=0,
                    action_observe=2,
                    action_unavailable=54,
                ),
                _browser_stage(
                    "concentration", 2, 0 if empty else 2, selected_executable=0, selected_observe=0 if empty else 2
                ),
            ],
        },
        "stage_snapshots": _browser_stage_snapshots(empty=empty),
        "supply_reason_counts": {
            "below_score_threshold": 54,
            "risk_veto": 2,
            "corporate_risk_window_unavailable": 1,
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


def _browser_stage_snapshots(*, empty: bool) -> list[dict[str, object]]:
    outputs = [5291, 5291, 5291, 1000, 1000, 1000, 500, 360, 350, 56, 20, 20, 20, 0 if empty else 2]
    return [
        {"stage": f"stage_{index}", "stage_order": index, "state": "completed", "output_count": output}
        for index, output in enumerate(outputs, start=1)
    ]


def _browser_stage(
    key: str,
    input_count: int,
    output_count: int | None,
    *,
    threshold: float | None = None,
    **details: int | tuple[float, float],
) -> dict[str, object]:
    metrics = [
        {"metric": name, "minimum": value[0], "maximum": value[1]}
        for name, value in details.items()
        if isinstance(value, tuple)
    ]
    facets = [
        {"key": name, "count": value, "total": input_count} for name, value in details.items() if isinstance(value, int)
    ]
    return {
        "key": key,
        "state": "completed",
        "input_count": input_count,
        "output_count": output_count,
        "metric_ranges": metrics,
        "threshold": threshold,
        "facets": facets,
        "reason_counts": [],
    }


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
        Board.MAIN,
        rank,
        name={
            "600009": "上海机场",
            "600001": "邯郸钢铁",
            "600010": "包钢股份",
            "600002": "齐鲁石化",
        }[code],
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
        """
        const header = document.querySelector('.app-header').getBoundingClientRect();
        const summary = document.querySelector('.summary-band').getBoundingClientRect();
        const controls = document.querySelector('.control-band').getBoundingClientRect();
        const layout = document.querySelector('#recommendation-layout').getBoundingClientRect();
        const rows = Array.from(document.querySelectorAll('#tableBody tr[data-code]'));
        return {
          actual: [window.innerWidth, window.innerHeight],
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
          ordered: header.bottom <= summary.top && summary.bottom <= controls.top && controls.bottom <= layout.top,
          equal_summary_heights: new Set(
            Array.from(document.querySelectorAll('.summary-band > .summary-item'))
              .map((item) => item.getBoundingClientRect().height),
          ).size === 1,
          cards: document.querySelectorAll('.summary-band > .summary-item').length,
          rows: rows.length,
          ranks: rows.map((row) => Number(row.cells[0].textContent)),
          tiers: rows.map((row) => row.cells[1].textContent.trim()),
          current_columns: rows[0]?.cells.length,
          classified_industry: rows[0]?.cells[2].textContent.includes('证监会行业 G56航空运输业'),
          score_labels_absent: !/本地|风险复核|最终/.test(rows[0]?.cells[8].textContent || ''),
          action_alignment: [getComputedStyle(document.querySelector('.action-heading')).textAlign,
            getComputedStyle(rows[0].cells[9]).textAlign],
          reason_alignment: [getComputedStyle(document.querySelector('.reason-heading')).textAlign,
            getComputedStyle(rows[0].cells[10]).textAlign],
          browser_errors: window.TraderDashboardDiagnostics.snapshot().browserErrors,
        };
        """,
    )
    screenshot_name = f"desktop-{width}x{height}.png"
    (output_dir / screenshot_name).write_bytes(base64.b64decode(str(_screenshot(base))))
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
        result.get("actual") == result.get("requested")
        and result.get("overflow") is False
        and result.get("ordered") is True
        and result.get("equal_summary_heights") is True
        and result.get("cards") == 4
        and result.get("rows") == 2
        and result.get("ranks") == [1, 2]
        and result.get("tiers") == ["回避", "回避"]
        and result.get("current_columns") == 11
        and result.get("classified_industry") is True
        and result.get("score_labels_absent") is True
        and result.get("action_alignment") == ["center", "left"]
        and result.get("reason_alignment") == ["center", "left"]
        and result.get("browser_errors") == []
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
                with OpenExternalHttp() as transport:
                    response = transport.get(
                        f"http://127.0.0.1:{port}/json/list", timeout=1, max_response_bytes=1024 * 1024
                    )
                    response.raise_for_status()
                    targets = response.json()
                if isinstance(targets, list):
                    page = next((target for target in targets if target.get("type") == "page"), None)
                    if isinstance(page, dict) and isinstance(page.get("webSocketDebuggerUrl"), str):
                        return page["webSocketDebuggerUrl"]
            except (OSError, json.JSONDecodeError):
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
        except (OSError, RuntimeError):
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
    method: Literal["GET", "POST", "DELETE"] = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, Any]:
    data = json.dumps(payload).encode() if payload is not None else None
    request = HttpRequest(
        method, url, 45, (("Content-Type", "application/json"),), data, max_response_bytes=32 * 1024 * 1024
    )
    try:
        with OpenExternalHttp() as transport:
            response = transport.execute(request)
            response.raise_for_status()
            parsed = response.json()
    except HttpStatusError as exc:
        raise RuntimeError(f"WebDriver HTTP {exc.status_code}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("WebDriver response must be an object")
    if parsed.get("value") and isinstance(parsed["value"], dict) and parsed["value"].get("error"):
        raise RuntimeError(str(parsed["value"]))
    return parsed


if __name__ == "__main__":
    raise SystemExit(main())
