#!/usr/bin/env python3
"""Measure production scheduler-to-SSE-to-Firefox DOM refresh intervals with deterministic quotes."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from werkzeug.serving import WSGIRequestHandler, make_server

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.application.cadence import CadencePlanner, CadencePolicy  # noqa: E402
from trader.application.decision_core import UnifiedDecisionIndex  # noqa: E402
from trader.application.decision_drafts import UnifiedDecisionDraftIndex  # noqa: E402
from trader.application.decision_observers import AsyncDecisionObserver  # noqa: E402
from trader.application.decision_queries import UnifiedDecisionQueries  # noqa: E402
from trader.application.decision_stream import (  # noqa: E402
    UnifiedDecisionEventStream,
    UnifiedPublishedEvent,
)
from trader.application.ports.runtime_status import V2InputQualityStatus  # noqa: E402
from trader.application.ports.v2_runtime import (  # noqa: E402
    SharedDeepSeekRuntimeContract,
    V2CycleRequest,
    V2DecisionUnavailableError,
    V2ResearchIntent,
    V2ResearchRuntimeStatus,
)
from trader.application.research_audit import V2CommittedResearchAudit  # noqa: E402
from trader.application.runtime import (  # noqa: E402
    RuntimeSupervisor,
    RuntimeSupervisorConfig,
    scheduler_interval_seconds,
)
from trader.application.schedule import phase_at, shanghai_now  # noqa: E402
from trader.application.shutdown import ShutdownDeadline, ShutdownStep  # noqa: E402
from trader.application.v2_runtime import V2RuntimeDependencies, V2SchedulerRuntime  # noqa: E402
from trader.domain.recommendation.decision_identity import (  # noqa: E402
    CommittedDecisionRecord,
    DecisionIdentity,
    DecisionItem,
    DecisionOverlay,
    DecisionQuote,
    ScoredDecision,
)
from trader.domain.recommendation.models import RecommendationAction, Strategy  # noqa: E402
from trader.infra.settings import load_runtime_settings  # noqa: E402
from trader.web import create_app  # noqa: E402
from trader.web.route_services import UnifiedWebServices, WebApiConfig  # noqa: E402


class _QuietRequestHandler(WSGIRequestHandler):
    def log(self, request_type: str, message: str, *args: object) -> None:
        del request_type, message, args


class _AdvancingClock:
    def __init__(self, start: datetime) -> None:
        self._start = start
        self._started: float | None = None

    def start(self) -> None:
        self._started = time.monotonic()

    def now(self) -> datetime:
        if self._started is None:
            return self._start
        return self._start + timedelta(seconds=time.monotonic() - self._started)


class _TradingCalendar:
    def is_trading_day(self, day: date) -> bool:
        del day
        return True


class _History:
    def load(self, strategy: Strategy, trade_date: date) -> CommittedDecisionRecord | None:
        del strategy, trade_date
        return None

    def list_dates(self, strategy: Strategy, *, limit: int = 31) -> tuple[date, ...]:
        del strategy, limit
        return ()


class _RecordingData:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._calls: list[dict[str, object]] = []

    def refresh(self, request: V2CycleRequest) -> None:
        strategy = request.strategy
        observed_at = request.observed_at
        with self._lock:
            self._calls.append(
                {
                    "wall": time.monotonic(),
                    "strategy": strategy.value,
                    "observed_at": observed_at.isoformat(),
                }
            )

    def refresh_task(self, _request) -> None:
        return

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            return [dict(call) for call in self._calls]


class _OverlayOnlyDecisions:
    def input_quality_status(self) -> tuple[V2InputQualityStatus, ...]:
        return ()

    def has_local_draft(self, strategy: Strategy, trade_date: date) -> bool:
        del strategy, trade_date
        return False

    def build_local(self, request: V2CycleRequest) -> DecisionIdentity | None:
        del request
        raise V2DecisionUnavailableError("measurement_score_disabled")

    def initial_overlay(self, decision: ScoredDecision) -> DecisionOverlay:
        quotes = tuple(item.quote for item in decision.items if item.quote is not None)
        return DecisionOverlay(decision.strategy, decision.trade_date, decision.version, decision.observed_at, quotes)

    def refreshed_overlay(
        self,
        decision: ScoredDecision,
        request: V2CycleRequest,
        previous: DecisionOverlay | None,
    ) -> DecisionOverlay | None:
        observed_at = request.observed_at
        input_version = request.input_version
        if previous is None or previous.observed_at >= observed_at:
            return None
        quotes = tuple(
            replace(
                quote,
                price=round((quote.price or 0.0) + 0.01, 2),
                source_time=observed_at,
                data_version=input_version,
            )
            for quote in previous.quotes
        )
        return DecisionOverlay(decision.strategy, decision.trade_date, decision.version, observed_at, quotes)

    def research_audit(self, version: str) -> V2CommittedResearchAudit | None:
        del version
        return None

    def research_intent(self, decision: ScoredDecision) -> V2ResearchIntent:
        raise AssertionError(f"measurement does not build research intents: {decision.version}")


class _Reviews:
    runtime_contract = SharedDeepSeekRuntimeContract(
        daily_physical_limit=168,
        shared_cache=True,
        shared_single_flight=True,
    )

    def build_hybrid(self, local: ScoredDecision, request: V2CycleRequest) -> ScoredDecision:
        del local, request
        raise AssertionError("measurement disables scoring and DeepSeek review")


class _NoopResearchRuntime:
    def start(self) -> bool:
        return True

    def stop(self, *, wait: bool, deadline: ShutdownDeadline | None = None) -> ShutdownStep:
        del wait, deadline
        return ShutdownStep("research", completed=True, timed_out=False)

    def observe(self, intent: object, request: object) -> bool:
        del intent, request
        return False

    def offer_due(self, at: datetime, phase: object, *, is_trading_day: bool) -> bool:
        del at, phase, is_trading_day
        return False

    def wait_until_idle(self, timeout_seconds: float) -> bool:
        del timeout_seconds
        return True

    def status(self) -> V2ResearchRuntimeStatus:
        return V2ResearchRuntimeStatus(state="idle")


class _Freezes:
    def freeze(self, strategy: Strategy, at: datetime, current: object) -> None:
        raise AssertionError(f"unexpected freeze during interval measurement: {strategy.value}:{at}:{current}")

    def freeze_close_fallback(
        self,
        strategy: Strategy,
        at: datetime,
        current: object,
        *,
        recovery_path: str,
        official_close_version: str,
    ) -> None:
        raise AssertionError(
            f"unexpected close fallback: {strategy.value}:{at}:{current}:{recovery_path}:{official_close_version}"
        )


class _Settlement:
    def settle(self, at: datetime) -> None:
        raise AssertionError(f"unexpected settlement during interval measurement: {at}")


def _research_factory(on_result: object) -> _NoopResearchRuntime:
    del on_result
    return _NoopResearchRuntime()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=65.0, help="measurement duration (default: 65)")
    parser.add_argument("--minimum-updates", type=int, default=3, help="minimum DOM price changes required to pass")
    parser.add_argument(
        "--strategy",
        choices=("today", "tomorrow", "d25"),
        default="tomorrow",
        help="dashboard strategy to observe",
    )
    parser.add_argument(
        "--simulated-start",
        default="2026-08-24T13:05:00+08:00",
        help="timezone-aware Shanghai trading time used by the production scheduler",
    )
    parser.add_argument(
        "--runtime-config",
        default=str(PROJECT_ROOT / "config" / "v2" / "runtime.json"),
        help="runtime config supplying the Web snapshot retention window",
    )
    parser.add_argument("--output", default="-", help="JSON output path, or - for stdout")
    return parser


def _validate(args: argparse.Namespace) -> datetime:
    if args.duration_seconds <= 0.0:
        raise ValueError("--duration-seconds must be positive")
    if args.minimum_updates < 1:
        raise ValueError("--minimum-updates must be positive")
    try:
        parsed = datetime.fromisoformat(args.simulated_start)
    except ValueError as exc:
        raise ValueError("--simulated-start must be an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("--simulated-start must include a timezone offset")
    return shanghai_now(parsed)


def _seed(index: UnifiedDecisionIndex, strategy: Strategy, at: datetime, code: str) -> None:
    quote = DecisionQuote(
        code,
        10.0,
        1.0,
        100_000_000.0,
        1.5,
        20_000_000_000.0,
        "fixture",
        at,
        f"seed:{code}",
    )
    item = DecisionItem(
        code,
        RecommendationAction.EXECUTABLE,
        True,
        1,
        80.0,
        80.0,
        80.0,
        (("local_score", 80.0),),
        (),
        "measurement",
        name=f"测量{code}",
        industry="测量行业",
        quote=quote,
    )
    decision = ScoredDecision(
        strategy,
        at.date(),
        1,
        at,
        "local",
        None,
        (("market", "seed"),),
        "config:measurement",
        "strategy:measurement",
        "fusion:measurement",
        (item,),
        (),
    )
    overlay = DecisionOverlay(strategy, at.date(), decision.version, at, (quote,))
    result = index.publish_scored(decision, overlay, expected_version=None)
    if not result.accepted:
        raise RuntimeError(f"cannot seed {strategy.value}: {result.reason}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 45.0,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"WebDriver HTTP {exc.code}: {detail[:300]}") from exc
    decoded = json.loads(raw.decode("utf-8")) if raw else {}
    if not isinstance(decoded, dict):
        raise RuntimeError("WebDriver response must be an object")
    value = decoded.get("value")
    if isinstance(value, dict) and value.get("error"):
        raise RuntimeError(f"WebDriver command failed: {str(value.get('error'))[:120]}")
    return decoded


def _wait_driver(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("geckodriver exited before becoming ready")
        try:
            response = _request_json(f"http://127.0.0.1:{port}/status", timeout=0.5)
        except (OSError, RuntimeError, urllib.error.URLError):
            time.sleep(0.05)
            continue
        if isinstance(response.get("value"), dict) and response["value"].get("ready"):
            return
        time.sleep(0.05)
    raise RuntimeError("geckodriver did not become ready")


def _execute(base: str, script: str) -> Any:
    response = _request_json(
        f"{base}/execute/sync",
        method="POST",
        payload={"script": script, "args": []},
    )
    return response.get("value")


def _wait(predicate: Any, *, timeout_seconds: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise RuntimeError("browser condition timed out")


def _intervals(values: list[float], *, divisor: float = 1.0) -> list[float]:
    return [round((current - previous) / divisor, 3) for previous, current in zip(values, values[1:], strict=False)]


def _interval_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"sample_count": 0, "p50_seconds": None, "p95_seconds": None, "maximum_seconds": None}
    ordered = sorted(values)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "sample_count": len(ordered),
        "p50_seconds": round(statistics.median(ordered), 3),
        "p95_seconds": round(ordered[p95_index], 3),
        "maximum_seconds": round(ordered[-1], 3),
    }


def _numeric_field(record: dict[str, object], field: str) -> float:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"measurement record field {field} must be numeric")
    return float(value)


def _write_report(report: dict[str, object], output: str) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def _run(
    args: argparse.Namespace,
    simulated_start: datetime,
    *,
    snapshot_retention_seconds: float,
    patch_to_paint_budget_ms: float,
) -> dict[str, object]:
    geckodriver = shutil.which("geckodriver")
    if geckodriver is None or shutil.which("firefox") is None:
        raise RuntimeError("Firefox and geckodriver are required")
    target_strategy = Strategy(args.strategy)
    target_codes = {Strategy.TODAY: "600001", Strategy.TOMORROW: "600002", Strategy.D25: "600003"}
    clock = _AdvancingClock(simulated_start)
    index = UnifiedDecisionIndex()
    for strategy, code in target_codes.items():
        _seed(index, strategy, simulated_start - timedelta(seconds=1), code)
    data = _RecordingData()
    events = UnifiedDecisionEventStream(history_size=64, client_queue_size=16, subscriber_limit=4)
    event_times: list[dict[str, object]] = []
    event_lock = threading.Lock()
    measurement_started = 0.0

    def publish_overlay(overlay: DecisionOverlay) -> UnifiedPublishedEvent:
        with event_lock:
            event_times.append(
                {
                    "wall": time.monotonic(),
                    "strategy": overlay.strategy.value,
                    "observed_at": overlay.observed_at.isoformat(),
                    "price": overlay.quotes[0].price if overlay.quotes else None,
                }
            )
        current = index.snapshot(overlay.strategy).current
        if not isinstance(current, ScoredDecision):
            raise RuntimeError("measurement overlay parent is unavailable")
        return events.publish_overlay(overlay, parent_content_hash=current.content_hash)

    runtime_settings = load_runtime_settings(args.runtime_config)
    cadence = CadencePlanner(
        CadencePolicy.from_seconds(runtime_settings.pipeline.cadence_seconds),
        started_at=simulated_start,
    )

    runtime = V2SchedulerRuntime(
        V2RuntimeDependencies(
            clock=clock,
            calendar=_TradingCalendar(),
            cadence=cadence,
            data=data,
            decisions=_OverlayOnlyDecisions(),
            reviews=_Reviews(),
            index=index,
            observer=AsyncDecisionObserver((), capacity=16, thread_name="measurement-observer"),
            freezes=_Freezes(),
            settlement=_Settlement(),
            research_factory=_research_factory,
            publish_decision=lambda _event: None,
            publish_overlay=publish_overlay,
        ),
        config_version="measurement",
        shutdown_timeout_seconds=5.0,
    )
    supervisor = RuntimeSupervisor(
        runtime,
        RuntimeSupervisorConfig(
            now=clock.now,
            initializers=(),
            interval_seconds=scheduler_interval_seconds,
            shutdown_timeout_seconds=5.0,
        ),
    )
    queries = UnifiedDecisionQueries(index, UnifiedDecisionDraftIndex(), _History(), clock)

    def status_provider() -> dict[str, object]:
        return {
            "status": "running",
            "runtime_started": True,
            "runtime_version": "measurement",
            "phase": phase_at(clock.now(), is_trading_day=True).value,
            "health": {"level": "normal", "issue_count": 0},
            "deepseek_budget": {"used": 0, "remaining": 168, "planned_limit": 168},
            "market_data": {"active_source": "fixture", "candidate_quote_latest_source": "fixture"},
        }

    app_port = _free_port()
    driver_port = _free_port()
    server = make_server(
        "127.0.0.1",
        app_port,
        create_app(
            services=UnifiedWebServices(
                queries,
                events,
                status_provider,
                WebApiConfig(
                    heartbeat_seconds=1.0,
                    snapshot_retention_seconds=snapshot_retention_seconds,
                ),
            )
        ),
        threaded=True,
        request_handler=_QuietRequestHandler,
    )
    server_thread = threading.Thread(target=server.serve_forever, name="measurement-web", daemon=True)
    server_started = False
    driver: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    supervisor_started = False
    browser_records: list[dict[str, object]] = []
    diagnostics: dict[str, object] = {}
    try:
        server_thread.start()
        server_started = True
        driver = subprocess.Popen(
            (geckodriver, "--host", "127.0.0.1", "--port", str(driver_port)),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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
        value = created.get("value")
        if not isinstance(value, dict) or not value.get("sessionId"):
            raise RuntimeError("WebDriver did not return a session id")
        session_id = str(value["sessionId"])
        base = f"http://127.0.0.1:{driver_port}/session/{session_id}"
        _request_json(f"{base}/url", method="POST", payload={"url": f"http://127.0.0.1:{app_port}/"})
        _wait(lambda: bool(_execute(base, "return Boolean(window.TraderDashboardDiagnostics);")))
        _execute(
            base,
            f'document.querySelector(".strategy-tab[data-strategy={target_strategy.value}]").click(); return true;',
        )
        code = target_codes[target_strategy]
        _wait(
            lambda: bool(
                _execute(base, f"return document.querySelector('#tableBody tr[data-code=\"{code}\"]') !== null;")
            )
        )
        _execute(
            base,
            f"""
            window.__intervalRecords = [];
            const initialRow = document.querySelector('#tableBody tr[data-code="{code}"]');
            window.__intervalLast = initialRow.cells[2].textContent.trim();
            window.__intervalObserver = new MutationObserver(() => {{
              const row = document.querySelector('#tableBody tr[data-code="{code}"]');
              if (!row) return;
              const price = row.cells[2].textContent.trim();
              if (price === window.__intervalLast) return;
              window.__intervalLast = price;
              window.__intervalRecords.push({{epochMs: Date.now(), perfMs: performance.now(), price}});
            }});
            window.__intervalObserver.observe(document.body, {{subtree: true, childList: true, characterData: true}});
            return window.__intervalLast;
            """,
        )
        clock.start()
        measurement_started = time.monotonic()
        if not supervisor.start():
            raise RuntimeError("runtime supervisor did not start")
        supervisor_started = True
        time.sleep(args.duration_seconds)
        raw_records = _execute(base, "return window.__intervalRecords;")
        raw_diagnostics = _execute(base, "return window.TraderDashboardDiagnostics.snapshot();")
        if isinstance(raw_records, list):
            browser_records = [record for record in raw_records if isinstance(record, dict)]
        if isinstance(raw_diagnostics, dict):
            diagnostics = raw_diagnostics
    finally:
        try:
            if supervisor_started:
                supervisor.stop(ShutdownDeadline.start(5.0))
        finally:
            try:
                if session_id is not None:
                    try:
                        _request_json(f"http://127.0.0.1:{driver_port}/session/{session_id}", method="DELETE")
                    except (OSError, RuntimeError, urllib.error.URLError):
                        pass
            finally:
                try:
                    if driver is not None:
                        driver.terminate()
                        try:
                            driver.wait(timeout=5.0)
                        except subprocess.TimeoutExpired:
                            driver.kill()
                            driver.wait(timeout=5.0)
                finally:
                    if server_started:
                        server.shutdown()
                    server.server_close()
                    if server_started:
                        server_thread.join(timeout=5.0)

    refresh_calls = data.snapshot()
    with event_lock:
        overlay_events = [dict(event) for event in event_times]
    target_refreshes = [
        _numeric_field(call, "wall") for call in refresh_calls if call["strategy"] == target_strategy.value
    ]
    target_events = [
        _numeric_field(event, "wall") for event in overlay_events if event["strategy"] == target_strategy.value
    ]
    browser_epochs = [_numeric_field(record, "epochMs") for record in browser_records]
    refresh_intervals = _intervals(target_refreshes)
    event_intervals = _intervals(target_events)
    browser_intervals = _intervals(browser_epochs, divisor=1000.0)
    browser_summary = _interval_summary(browser_intervals)
    observed_maximum = browser_summary["maximum_seconds"]
    retention_margin = (
        round(snapshot_retention_seconds - observed_maximum, 3) if isinstance(observed_maximum, (int, float)) else None
    )
    browser_errors = diagnostics.get("browserErrors")
    raw_patch_latency = diagnostics.get("patchToPaint")
    patch_latency = raw_patch_latency if isinstance(raw_patch_latency, dict) else {}
    patch_p95 = patch_latency.get("p95_ms")
    patch_latency_passed = (
        isinstance(patch_p95, (int, float))
        and not isinstance(patch_p95, bool)
        and float(patch_p95) <= patch_to_paint_budget_ms
    )
    passed = (
        len(browser_records) >= args.minimum_updates
        and isinstance(browser_errors, list)
        and not browser_errors
        and retention_margin is not None
        and retention_margin > 0
        and patch_latency_passed
    )
    return {
        "schema_version": "web-refresh-interval-v2",
        "passed": passed,
        "strategy": target_strategy.value,
        "simulated_start": simulated_start.isoformat(),
        "duration_seconds": args.duration_seconds,
        "refresh": {
            "timestamps_seconds": [round(value - measurement_started, 3) for value in target_refreshes],
            "intervals_seconds": refresh_intervals,
            "summary": _interval_summary(refresh_intervals),
        },
        "sse_overlay": {
            "timestamps_seconds": [round(value - measurement_started, 3) for value in target_events],
            "intervals_seconds": event_intervals,
            "summary": _interval_summary(event_intervals),
        },
        "browser_dom": {
            "records": browser_records,
            "intervals_seconds": browser_intervals,
            "summary": browser_summary,
        },
        "browser_patch_to_paint": {
            "summary": patch_latency,
            "budget_ms": patch_to_paint_budget_ms,
            "passed": patch_latency_passed,
        },
        "web_snapshot_retention": {
            "configured_seconds": snapshot_retention_seconds,
            "observed_maximum_interval_seconds": observed_maximum,
            "remaining_margin_seconds": retention_margin,
            "covers_observed_interval": retention_margin is not None and retention_margin > 0,
        },
        "browser_diagnostics": diagnostics,
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    report: dict[str, object]
    try:
        simulated_start = _validate(args)
        settings = load_runtime_settings(args.runtime_config)
        report = _run(
            args,
            simulated_start,
            snapshot_retention_seconds=settings.api.web_snapshot_retention_seconds,
            patch_to_paint_budget_ms=settings.performance_budgets.latency_p95_ms["browser_patch_to_paint"],
        )
    except (OSError, RuntimeError, TypeError, ValueError, urllib.error.URLError) as exc:
        report = {
            "schema_version": "web-refresh-interval-v2",
            "passed": False,
            "error": type(exc).__name__,
            "message": str(exc)[:300] or "no additional detail",
        }
    _write_report(report, args.output)
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
