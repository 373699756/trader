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
    UnifiedFreezeDraftIndex,
)
from trader.recommendation.application.pipeline.freeze_publish.event_stream import (  # noqa: E402
    UnifiedFreezeEventStream,
)
from trader.recommendation.application.pipeline.freeze_publish.read_only_queries import (  # noqa: E402
    FreezeQuerySources,
    UnifiedFreezeQueries,
)
from trader.recommendation.application.pipeline.freeze_publish.snapshot_publisher import (  # noqa: E402
    UnifiedFreezeIndex,
)
from trader.recommendation.application.ports.freeze_records import CommittedFreezeRecord  # noqa: E402
from trader.recommendation.application.ports.read_only_queries import InputQualityStatus, SupplySummary  # noqa: E402
from trader.recommendation.domain.evidence.pipeline import (  # noqa: E402
    PIPELINE_STAGES,
    PipelineFacet,
    PipelineStage,
    PipelineStageSnapshot,
    PipelineStageStatus,
    RecommendationPipelineStatus,
    SourceHealth,
    SourceHealthState,
    StageState,
)
from trader.recommendation.domain.market.models import Board, ModelIndustryReference  # noqa: E402
from trader.recommendation.domain.publication.freeze_identity import (  # noqa: E402
    FreezeItem,
    FreezeQuote,
    LongProjection,
    LongProjectionItem,
    ScoredFreeze,
    SelectionDiagnostics,
)
from trader.recommendation.domain.publication.models import RecommendationAction, Strategy  # noqa: E402
from trader.recommendation.domain.publication.preview import PreviewContext  # noqa: E402
from trader.recommendation.infra.status_projection import InputQualityPayload  # noqa: E402
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
    def load(self, strategy: Strategy, trade_date: date) -> CommittedFreezeRecord | None:
        del strategy, trade_date
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        del strategy, limit
        return ()


class _PreviewClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 16, 17, 1, tzinfo=_SHANGHAI)


class _PreviewBrowserFixtures:
    def __init__(self, items: tuple[FreezeItem, ...], pipeline: RecommendationPipelineStatus) -> None:
        started_at = datetime(2026, 8, 16, 17, 0, tzinfo=_SHANGHAI)
        self.context = PreviewContext("browser-preview", date(2026, 8, 14), started_at)
        self.generated_at = started_at.replace(second=5)
        self._state: Literal["waiting_inputs", "running", "ready", "failed"] = "waiting_inputs"
        self._error: str | None = None
        self._published_count = 0
        self._indexes = {profile: UnifiedFreezeIndex() for profile in ("v1", "v2", "v3", "v5")}
        completed_items = tuple(
            replace(item, quote=replace(item.quote, source_time=self.context.market_as_of))
            if item.quote is not None
            else item
            for item in items
        )
        self._freezes = {
            (profile, strategy): ScoredFreeze(
                strategy,
                started_at.date(),
                1,
                self.generated_at,
                "local",
                None,
                self._input_versions(profile),
                "config:browser-preview",
                "strategy:browser-preview",
                "fusion:browser-preview",
                completed_items,
                (),
                pipeline=pipeline,
                preview_context=self.context,
            )
            for profile in self._indexes
            for strategy in (Strategy.TOMORROW, Strategy.D25)
        }
        self.services = ProfiledWebServices(
            {
                profile: UnifiedWebServices(
                    UnifiedFreezeQueries(index, UnifiedFreezeDraftIndex(), _BrowserHistory(), _PreviewClock()),
                    UnifiedFreezeEventStream(),
                    self._status,
                    profile_id=profile,
                )
                for profile, index in self._indexes.items()
            },
            default_profile="v2",
        )

    @property
    def published_count(self) -> int:
        return self._published_count

    @property
    def event_counts(self) -> tuple[int, ...]:
        return tuple(service.events.status().sequence for service in self.services.profiles.values())

    def set_state(
        self, state: Literal["waiting_inputs", "running", "ready", "failed"], error: str | None = None
    ) -> None:
        self._state = state
        self._error = error

    def publish(self) -> None:
        if self._published_count:
            raise RuntimeError("preview browser fixture must only compute once")
        self._state = "running"
        for (profile, _), freeze in self._freezes.items():
            result = self._indexes[profile].publish(freeze, expected_version=None)
            if not result.accepted or result.event is None:
                raise RuntimeError("preview browser fixture publication failed")
            self.services.profiles[profile].events.publish_committed(result.event)
            self._published_count += 1

    def _status(self) -> dict[str, object]:
        return {
            "status": "running",
            "runtime_started": True,
            "phase": "after_close",
            "run_mode": "preview",
            "preview_run_id": self.context.run_id,
            "market_trade_date": self.context.market_trade_date.isoformat(),
            "market_as_of": self.context.market_as_of.isoformat(),
            "preview_started_at": self.context.started_at.isoformat(),
            "preview_state": self._state,
            "preview_error": self._error,
            "scheduler": {"lanes": [], "input_quality": {}},
            "market_data": {"latest_source": "fixture"},
            "deepseek": {"enabled": False},
            "health": {"level": "normal", "issue_count": 0},
        }

    def _input_versions(self, profile: str) -> tuple[tuple[str, str], ...]:
        if profile != "v1":
            identity = (("score_scale", "weighted_evidence_quality_0_100"), ("score_model", f"{profile}:browser"))
            if profile == "v5":
                identity += (("execution_mode", "shadow"), ("production_authority", "false"))
            return identity
        return (
            ("score_scale", "learned_rank_percentile_0_100"),
            ("score_model_id", "learned_rank"),
            ("score_model", "learned_rank:" + "b" * 64),
            ("score_contract_hash", "a" * 64),
            ("rank_population", "learned-rank-population:" + "c" * 64),
            ("execution_mode", "production"),
            ("production_authority", "true"),
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--filter-counts-only", action="store_true", help="check filter disposition counts")
    parser.add_argument("--preview-only", action="store_true", help="check ephemeral preview through HTTP and SSE")
    parser.add_argument("--chrome-binary", type=Path, help="use an explicit Chrome binary for desktop acceptance")
    args = parser.parse_args()
    try:
        report = _run(
            args.output.parent,
            filter_counts_only=args.filter_counts_only,
            preview_only=args.preview_only,
            chrome_binary=args.chrome_binary,
        )
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


def _run(
    output_dir: Path,
    *,
    filter_counts_only: bool = False,
    preview_only: bool = False,
    chrome_binary: Path | None = None,
) -> dict[str, object]:
    driver_binary = shutil.which("geckodriver")
    app_port = _free_port()
    preview = (
        _PreviewBrowserFixtures((_observation_item("600001", rank=1, final_score=74.0),), _browser_pipeline())
        if preview_only
        else None
    )
    services, publish_empty_draft = _browser_services() if preview is None else (preview.services, preview.publish)
    server = make_server("127.0.0.1", app_port, CreateApp(services=services), threaded=True)
    driver_port = _free_port()
    server_thread = threading.Thread(target=server.serve_forever, name="browser-fixture", daemon=True)
    server_thread.start()
    driver: subprocess.Popen[str] | None = None
    chrome: _ChromeSession | None = None
    session_id: str | None = None
    try:
        if chrome_binary is None and driver_binary is not None and shutil.which("firefox") is not None:
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
            selected_chrome = (
                shutil.which(str(chrome_binary))
                if chrome_binary is not None
                else shutil.which("google-chrome") or shutil.which("chromium")
            )
            if selected_chrome is None:
                raise RuntimeError("Firefox/geckodriver or Google Chrome is required for the desktop gate")
            chrome = _ChromeSession(selected_chrome, driver_port)
            base = chrome
            browser_name = "chrome-headless"
        _navigate(base, f"http://127.0.0.1:{app_port}/")
        _wait(lambda: bool(_execute(base, "return Boolean(window.TraderDashboardDiagnostics);")), "dashboard readiness")
        if preview is not None:
            return _preview_acceptance(base, preview, output_dir, browser_name)
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
        profile_switches = []
        for profile in ("v1", "v5", "v3"):
            _execute(base, f'document.querySelector(".profile-tab[data-profile={profile}]").click(); return true;')
            _wait(
                lambda profile=profile: bool(
                    _execute(
                        base,
                        f"""
                    return ['status', 'freezes/d25/dates', 'freezes/d25/current'].every((path) =>
                      performance.getEntriesByType('resource').some((entry) =>
                        entry.name.includes('/api/' + path) && entry.name.includes('profile={profile}')))
                      && document.querySelectorAll('#tableBody tr[data-code]').length === 2;
                """,
                    )
                ),
                f"{profile} profile requests and rows",
            )
            profile_switches.append(
                _execute(
                    base,
                    """
                return {selected: document.querySelector('.profile-tab[aria-selected=true]')?.dataset.profile,
                  selected_count: document.querySelectorAll('.profile-tab[aria-selected=true]').length,
                  before_date: document.querySelector('.profile-control').getBoundingClientRect().right
                    <= document.querySelector('.date-control').getBoundingClientRect().left,
                  rows: document.querySelectorAll('#tableBody tr[data-code]').length};
            """,
                )
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
        close_chain = _close_chain(base, output_dir)
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
                isinstance(headers := item.get("headers"), list)
                and headers[:3] == ["排名", "状态", "股票"]
                and item.get("codes") == expected_codes
                and item.get("ranks") == [1, 2]
                and item.get("tiers") == ["关注", "关注"]
                and item.get("tier_colors_distinct") is True
                and item.get("cards") == 3
                and item.get("budget_hidden") is True
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
            and close_chain.get("passed") is True
            and profile_switches
            == [
                {"selected": profile, "selected_count": 1, "before_date": True, "rows": 2}
                for profile in ("v1", "v5", "v3")
            ]
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
            "browser_version": _execute(base, "return navigator.userAgent;"),
            "snapshots": snapshots,
            "avoided": avoided,
            "observation_copy": observation_copy,
            "close_chain": close_chain,
            "profile_switches": profile_switches,
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


def _preview_acceptance(
    base: str | _ChromeSession,
    fixture: _PreviewBrowserFixtures,
    output_dir: Path,
    browser_name: str,
) -> dict[str, object]:
    def publication_text() -> str:
        return str(_execute(base, 'return document.querySelector("#publicationStatus")?.textContent || "";'))

    def refresh() -> None:
        _execute(base, 'document.querySelector("#refreshButton").click(); return true;')

    _wait(lambda: publication_text() == "测试预览 · 等待输入", "preview waiting inputs")
    _wait(
        lambda: _execute(base, 'return document.querySelector("#streamStatus").textContent;') == "实时",
        "preview SSE connection",
    )
    fixture.publish()
    _wait(
        lambda: _integer(_execute(base, 'return document.querySelectorAll("#tableBody tr[data-code]").length;')) == 1,
        "preview SSE rows",
    )
    sse = _execute(base, "return window.TraderDashboardDiagnostics.snapshot().recommendationPatchesApplied;")
    fixture.set_state("ready")
    refresh()
    _wait(lambda: publication_text() == "测试预览 · 已完成", "preview completed status")
    before = _execute(
        base,
        "return fetch('/api/freezes/tomorrow/current?profile=v2').then(r => r.json()).then(p => p.publication_id);",
    )
    refresh()
    for _ in range(2):
        _execute(base, "window.__previewReloadPending = true; window.location.reload(); return true;")
        _wait(
            lambda: bool(
                _execute(
                    base,
                    """return !window.__previewReloadPending
                      && document.querySelector('#publicationStatus')?.textContent === '测试预览 · 已完成';""",
                )
            ),
            "preview reload completed status",
        )
    after = _execute(
        base,
        "return fetch('/api/freezes/tomorrow/current?profile=v2').then(r => r.json()).then(p => p.publication_id);",
    )
    profiles = []
    for profile in ("v1", "v3", "v5", "v2"):
        _execute(base, f'document.querySelector(".profile-tab[data-profile={profile}]").click(); return true;')
        _wait(
            lambda profile=profile: bool(
                _execute(
                    base,
                    f"""const selected = document.querySelector('.profile-tab[aria-selected=true]');
                      return selected?.dataset.profile === '{profile}'
                      && document.querySelector('#publicationMeta').textContent.includes('2026-08-14')
                      && document.querySelector('#sixSeatMeta').textContent.includes('测试预览');""",
                )
            ),
            f"{profile} preview display",
        )
        profiles.append(
            _execute(
                base,
                """return {profile: document.querySelector('.profile-tab[aria-selected=true]').dataset.profile,
                  publication: document.querySelector('#publicationStatus').textContent,
                  meta: document.querySelector('#publicationMeta').textContent,
                  six_seat_meta: document.querySelector('#sixSeatMeta').textContent};""",
            )
        )
    fixture.set_state("failed", "production_model_contract_conflict")
    refresh()
    _wait(lambda: publication_text() == "测试预览 · 已阻塞", "preview blocked state retains local result")
    failed = _execute(base, "return document.querySelector('#publicationMeta').textContent;")
    errors = _execute(base, "return window.TraderDashboardDiagnostics.snapshot().browserErrors;")
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "preview.png").write_bytes(base64.b64decode(str(_screenshot(base))))
    learned_rank = next((item for item in profiles if isinstance(item, dict) and item.get("profile") == "v1"), {})
    shadow = next((item for item in profiles if isinstance(item, dict) and item.get("profile") == "v5"), {})
    passed = (
        isinstance(sse, int)
        and sse >= 1
        and before == after
        and isinstance(before, str)
        and bool(before)
        and fixture.published_count == 8
        and fixture.event_counts == (2, 2, 2, 2)
        and "Shadow" not in str(learned_rank.get("meta"))
        and "Shadow" in str(shadow.get("meta"))
        and "production_model_contract_conflict" in str(failed)
        and errors == []
    )
    return {
        "schema_version": REPORT_SCHEMA,
        "passed": passed,
        "browser": browser_name,
        "preview": {
            "profiles": profiles,
            "sse_patches": sse,
            "refresh_version_unchanged": before == after,
            "published_count": fixture.published_count,
            "event_counts": fixture.event_counts,
            "blocked_meta": failed,
            "browser_errors": errors,
            "screenshot": "preview.png",
        },
        "external_network_calls": 0,
    }


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
            const stages = ['local_data', 'static_filter', 'dynamic_market', 'dynamic_filter',
              'market_features', 'model_filter', 'local_score', 'local_risk', 'fixed_selection',
              'risk_review', 'action_publish'];
            window.TraderStatusView.renderObservationStages({observationStageList: list}, stages.map(stage =>
              ({stage, state: 'degraded', input_count: 4391, output_count: 129,
                rejected_count: 1079, pending_count: 3182, failed_count: 1, latency_ms: 1000})
            ), null, false, []);
            const row = list.querySelector('[data-stage-keys="dynamic_filter"]');
            const text = row.querySelector('span');
            const expected = '4391 → 129 · 淘汰 1079 · 待补 3182 · 失败 1';
            return {text: text.textContent, passed: text.textContent.includes(expected)
              && text.scrollWidth <= text.clientWidth && row.getBoundingClientRect().width > 0};
        """,
        )
        if not isinstance(result, dict):
            raise RuntimeError("browser filter counts must be an object")
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
          cards: document.querySelectorAll('.summary-band > .summary-item:not([hidden])').length,
          budget_hidden: document.querySelector('#budgetPanel').hidden,
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
        const stage = list.querySelector('[data-stage-keys="dynamic_market"]');
        if (!stage?.querySelector('.observation-stage-detail')?.textContent.trim())
          return {passed: false, reason: 'no_realtime_stage_detail'};
        const button = stage.querySelector('button[data-stage-toggle]');
        const index = stage.querySelector('.observation-stage-index');
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
        range.selectNodeContents(button);
        selection.addRange(range);
        const visibleText = selection.toString();
        const title = button.querySelector('strong').textContent.trim();
        const summary = button.querySelector('span').textContent.trim();
        button.click();
        const headerStillExpanded = stage.classList.contains('is-expanded');
        selection.removeAllRanges();
        range.selectNodeContents(index);
        selection.addRange(range);
        const indexSelected = selection.toString().trim() === '03';
        selection.removeAllRanges();
        document.querySelector('#observationDrawerClose').click();
        return {passed: button.tagName === 'BUTTON' && !button.contains(detail)
          && document.querySelector('#observationDrawerTitle').textContent === '观察 · 评分链路（只读诊断）'
          && getComputedStyle(document.querySelector('#observationDrawerTitle')).fontSize === '16px'
          && getComputedStyle(detail).userSelect === 'text'
          && getComputedStyle(button).userSelect === 'text'
          && getComputedStyle(index).userSelect === 'text'
          && visibleText.includes(title) && visibleText.includes(summary)
          && indexSelected && headerStillExpanded
          && selected.length > 0 && stillExpanded && preserved,
          selected_length: selected.length, index_selected: indexSelected,
          header_still_expanded: headerStillExpanded, still_expanded: stillExpanded, preserved};
        """,
    )
    if not isinstance(result, dict):
        raise RuntimeError("browser observation copyability result must be an object")
    return result


def _close_chain(base: str | _ChromeSession, output_dir: Path) -> dict[str, object]:
    result = _execute(
        base,
        """
        return (async () => {
          const response = await fetch('/api/freezes/tomorrow/current?profile=v2');
          const published = {...await response.json(), historical: false};
          const full = published.pipeline?.stage_snapshots;
          if (!Array.isArray(full) || full.length !== 11) return {passed: false, reason: 'missing_api_stages'};
          const els = Object.fromEntries(Array.from(document.querySelectorAll('[id]'), (el) => [el.id, el]));
          const list = els.observationStageList;
          const pending = {...published, status: 'not_ready', frozen: false, pipeline: null, items: [],
            draft: null, coverage: {}};
          const quality = {summary: {trade_date: published.trade_date},
            pipeline: published.pipeline, stage_snapshots: full.slice(0, 3)};
          const next = {...quality, stage_snapshots: quality.stage_snapshots.map((stage) => ({...stage,
            output_batch_id: 'next:' + stage.output_batch_id,
            output_count: stage.stage === 'dynamic_market' ? 3333 : stage.output_count,
            rejected_count: stage.stage === 'dynamic_market' ? 667 : stage.rejected_count}))};
          const draw = (payload, input, phase = 'after_close') => {
            window.TraderStatusView.renderSummary(els, payload, [], 'closed_market', null,
              window.TraderSelection, window.TraderRender,
              {phase, scheduler: {input_quality: input ? {tomorrow: input} : {}}});
            return {count: list.querySelectorAll('.observation-stage').length,
              dynamic: list.querySelector('[data-stage-keys="dynamic_market"] button span')?.textContent,
              unknown: (list.textContent.match(/缺失字段不代表零/g) || []).length,
              next_round: els.funnelStages.textContent.includes('新输入轮正在处理')};
          };
          const cases = [];
          const check = (name, payload, input, phase, expected) => {
            const actual = draw(payload, input, phase);
            cases.push({name, ...actual, passed: actual.count === 11 && expected(actual)});
          };
          check('morning_hot', published, null, 'before_close', (s) => s.unknown === 0);
          check('morning_cold', pending, null, 'before_close', (s) => s.unknown === 11);
          check('midday_cold', pending, quality, 'before_close', (s) => s.unknown === 8
            && s.dynamic.includes('4000 → 4000'));
          check('close_boundary', pending, quality, 'after_close', (s) => s.unknown === 8);
          check('after_close_progress', pending, next, 'after_close', (s) => s.unknown === 8
            && s.dynamic.includes('4000 → 3333'));
          const frozen = {...published, frozen: true};
          check('formal_hit', frozen, next, 'after_close', (s) => s.unknown === 0
            && s.dynamic.includes('4000 → 4000') && !s.next_round);
          check('close_fallback', {...frozen, freeze_kind: 'close_fallback'}, next, 'after_close',
            (s) => s.unknown === 0 && !s.next_round);
          check('history', {...frozen, historical: true, view: 'history'}, next, 'after_close',
            (s) => s.unknown === 0 && !s.next_round);
          check('missing_history', {...pending, historical: true, view: 'history'}, next, 'after_close',
            (s) => s.unknown === 11);
          check('different_date', pending, {...quality, summary: {trade_date: '2000-01-01'}}, 'after_close',
            (s) => s.unknown === 11);
          check('different_strategy', {...pending, strategy: 'd25'}, quality, 'after_close',
            (s) => s.unknown === 11);
          check('next_round_same_date', published, next, 'before_close', (s) => s.next_round);
          check('next_round_different_date', published,
            {...next, summary: {trade_date: '2000-01-01'}}, 'before_close', (s) => !s.next_round);
          const legacy = {current_stage: 'final_selection', stages: [{key: 'input_readiness'}],
            stage_snapshots: [{stage: 'stage_1', output_count: 5291}]};
          check('legacy_history', {...frozen, historical: true, view: 'history', pipeline: legacy}, next,
            'after_close', (s) => s.unknown === 11 && !list.textContent.includes('5291'));
          draw(frozen, next);
          if (!els.observationDrawer.classList.contains('is-open')) els.errorDetailsButton.click();
          await Promise.all(els.observationDrawer.getAnimations().map((animation) => animation.finished));
          const stage = list.querySelector('[data-stage-keys="dynamic_market"]');
          const button = stage.querySelector('button');
          if (!stage.classList.contains('is-expanded')) button.click();
          const detail = stage.querySelector('.observation-stage-detail');
          const range = document.createRange();
          range.selectNodeContents(detail);
          const selection = window.getSelection();
          selection.removeAllRanges();
          selection.addRange(range);
          const copied = selection.toString();
          draw(frozen, next);
          const preserved = stage.isConnected && copied.length > 0 && selection.toString() === copied;
          selection.removeAllRanges();
          draw(pending, next);
          const expanded = list.querySelector('[data-stage-keys="dynamic_market"]');
          const updated = expanded.classList.contains('is-expanded')
            && expanded.querySelector('button').getAttribute('aria-expanded') === 'true'
            && expanded.textContent.includes('4000 → 3333');
          draw(frozen, next);
          const readable = Array.from(list.querySelectorAll('button span')).every((text) =>
            text.getBoundingClientRect().width > 0 && text.scrollWidth <= text.clientWidth
            && text.getBoundingClientRect().right <= window.innerWidth);
          const durations = list.textContent.includes('0 ms') && list.textContent.includes('1.25 s')
            && list.textContent.includes('未测量') && list.textContent.includes('沿用启动结果');
          const long = draw({...published, strategy: 'long'}, next);
          const longExcluded = long.count === 0 && list.textContent.includes('不经过荐股评分链路');
          draw(frozen, next);
          list.querySelector('[data-stage-keys="dynamic_market"] button').click();
          const errors = window.TraderDashboardDiagnostics.snapshot().browserErrors;
          return {passed: cases.every((item) => item.passed) && preserved && updated && readable
            && durations && longExcluded && errors.length === 0,
            cases, selection_preserved: preserved, expanded_after_update: updated, readable,
            durations, long_excluded: longExcluded, browser_errors: errors};
        })();
        """,
    )
    if not isinstance(result, dict):
        raise RuntimeError("browser close-chain result must be an object")
    (output_dir / "close-chain.png").write_bytes(base64.b64decode(str(_screenshot(base))))
    _execute(base, "document.querySelector('#observationDrawerClose').click(); return true;")
    return result


def _browser_services() -> tuple[ProfiledWebServices, Callable[[], None]]:
    index = UnifiedFreezeIndex()
    drafts = UnifiedFreezeDraftIndex()
    freezes: dict[Strategy, ScoredFreeze] = {}
    for strategy, codes in (
        (Strategy.TOMORROW, ("600009", "600001")),
        (Strategy.D25, ("600010", "600002")),
    ):
        items = tuple(
            _observation_item(code, rank=rank, final_score=score)
            for rank, (code, score) in enumerate(zip(codes, (74.0, 72.0), strict=True), start=1)
        )
        freeze = ScoredFreeze(
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
            pipeline=_browser_pipeline(),
        )
        freezes[strategy] = freeze
    if not drafts.publish(freezes[Strategy.D25]).accepted:
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
            result = drafts.publish(freezes[Strategy.TOMORROW])
            if not result.accepted:
                raise RuntimeError(f"browser tomorrow draft publication failed: {result.reason}")
        input_quality = _browser_input_quality(empty=empty_draft_published) if status_calls >= 2 else {}
        return {
            "status": "running",
            "runtime_started": True,
            "phase": "before_close",
            "deepseek": {"enabled": False},
            "deepseek_budget": {"used": 0, "remaining": 168, "planned_limit": 71},
            "market_data": {
                "latest_source": "eastmoney",
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
        empty = ScoredFreeze(
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
            pipeline=_browser_pipeline(empty=True),
        )
        result = index.publish(empty, expected_version=None)
        if not result.accepted:
            raise RuntimeError(f"browser empty freeze publication failed: {result.reason}")
        empty_draft_published = True

    return (
        ProfiledWebServices(
            {
                profile: UnifiedWebServices(
                    UnifiedFreezeQueries(
                        index,
                        drafts,
                        _BrowserHistory(),
                        _BrowserClock(),
                        FreezeQuerySources(industry_classifications=_BrowserIndustryClassifications()),
                    ),
                    UnifiedFreezeEventStream(),
                    status_provider,
                    profile_id=profile,
                )
                for profile in ("v1", "v2", "v3", "v5")
            },
            "v2",
        ),
        publish_empty_draft,
    )


def _browser_input_quality(*, empty: bool = False) -> dict[str, object]:
    pipeline = _browser_pipeline(empty=empty)
    return InputQualityPayload(
        tuple(
            InputQualityStatus(
                strategy,
                "not_ready",
                False,
                SupplySummary(_NOW.date(), 360, 360, 0, 240, "tencent", _NOW.replace(hour=10, minute=0), 74.25),
                pipeline,
                pipeline.stage_snapshots,
                population_count=5291,
                data_pending_count=4291,
                candidate_optional_reason_counts=(("missing_listing_date", 221), ("missing_listing_age_sessions", 65)),
                supply_reason_counts=(("risk_veto", 2), ("corporate_risk_window_unavailable", 1), ("stale_quote", 1)),
                primary_blocker="security_master_coverage_incomplete",
            )
            for strategy in (Strategy.TOMORROW, Strategy.D25)
        )
    )


def _browser_pipeline(*, empty: bool = False) -> RecommendationPipelineStatus:
    observed_at = _NOW.replace(hour=11, minute=15)
    outputs = (5291, 4000, 4000, 1000, 400, 360, 360, 360, 2, 2, 2)
    snapshots = tuple(
        PipelineStageSnapshot(
            stage,
            index + 1,
            f"browser:{index}",
            f"browser:{index + 1}",
            observed_at,
            StageState.READY,
            outputs[index - 1] if index else outputs[0],
            output,
            outputs[index - 1] - output if index and stage is not PipelineStage.FIXED_SELECTION else 0,
            0,
            0,
            (),
            SourceHealth(SourceHealthState.READY, 1, 1, observed_at, 0.0),
            None if index < 2 else 1250 if index == 2 else 0 if index == 3 else 10,
            False,
            not_selected_count=358 if stage is PipelineStage.FIXED_SELECTION else 0,
            operation_durations=(("startup_reuse", None),) if index < 2 else (),
        )
        for index, (stage, output) in enumerate(zip(PIPELINE_STAGES, outputs, strict=True))
    )
    return RecommendationPipelineStatus(
        "action_publish",
        tuple(
            PipelineStageStatus(
                snapshot.stage.value,
                "completed",
                snapshot.input_count,
                snapshot.output_count,
                facets=(
                    PipelineFacet("action_executable", 0),
                    PipelineFacet("action_observe", 0 if empty else 2),
                    PipelineFacet("action_unavailable", 2 if empty else 0),
                )
                if snapshot.stage is PipelineStage.ACTION_PUBLISH
                else (),
            )
            for snapshot in snapshots
        ),
        snapshots,
        elapsed_ms=2000,
    )


def _observation_item(code: str, *, rank: int, final_score: float) -> FreezeItem:
    return FreezeItem(
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
        quote=FreezeQuote(
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
        const cards = Array.from(document.querySelectorAll('.summary-band > .summary-item:not([hidden])'));
        const cardWidths = cards.map((item) => item.getBoundingClientRect().width);
        return {
          actual: [window.innerWidth, window.innerHeight],
          overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
          ordered: header.bottom <= summary.top && summary.bottom <= controls.top && controls.bottom <= layout.top,
          equal_summary_heights: new Set(cards.map((item) => item.getBoundingClientRect().height)).size === 1,
          equal_summary_widths: Math.max(...cardWidths) - Math.min(...cardWidths) <= 1,
          cards: cards.length,
          budget_hidden: document.querySelector('#budgetPanel').hidden,
          rows: rows.length,
          ranks: rows.map((row) => Number(row.cells[0].textContent)),
          tiers: rows.map((row) => row.cells[1].textContent.trim()),
          current_columns: rows[0]?.cells.length,
          headers_centered: Array.from(document.querySelectorAll('#tableHead th')).every((cell) =>
            getComputedStyle(cell).textAlign === 'center'),
          column_widths: Array.from(document.querySelectorAll('#tableColumns col')).slice(2, 4)
            .map((column) => column.style.width),
          stock_only: !rows[0]?.cells[2].textContent.includes('证监会行业'),
          classified_industry: rows[0]?.cells[3].textContent.includes('证监会行业 G56航空运输业'),
          board_industry_alignment: getComputedStyle(rows[0].cells[3]).textAlign,
          market_value_alignments: Array.from(rows[0].cells).slice(4, 9)
            .map((cell) => getComputedStyle(cell).textAlign),
          score_labels_absent: !/本地|风险复核|最终/.test(rows[0]?.cells[9].textContent || ''),
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
        and result.get("equal_summary_widths") is True
        and result.get("cards") == 3
        and result.get("budget_hidden") is True
        and result.get("rows") == 2
        and result.get("ranks") == [1, 2]
        and result.get("tiers") == ["回避", "回避"]
        and result.get("current_columns") == 11
        and result.get("headers_centered") is True
        and result.get("column_widths") == ["131px", "161px"]
        and result.get("stock_only") is True
        and result.get("classified_industry") is True
        and result.get("board_industry_alignment") == "left"
        and result.get("market_value_alignments") == ["center"] * 5
        and result.get("score_labels_absent") is True
        and result.get("reason_alignment") == ["center", "center"]
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
        deadline = time.monotonic() + 45
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
                    debugger_url = page.get("webSocketDebuggerUrl") if isinstance(page, dict) else None
                    if isinstance(debugger_url, str):
                        return debugger_url
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
