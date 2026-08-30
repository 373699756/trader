#!/usr/bin/env python3
"""Run bounded Trader diagnostics through one command and emit a sanitized combined report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"

Profile = Literal[
    "web",
    "history",
    "security-master",
    "tencent",
    "tushare",
    "research",
    "browser",
    "performance",
    "runtime",
    "sources",
    "live",
    "full",
]
CheckStatus = Literal["passed", "degraded", "failed"]

_PROFILE_CHECKS: Mapping[Profile, tuple[str, ...]] = {
    "web": ("web_health",),
    "history": ("history_sources",),
    "security-master": ("exchange_security_master",),
    "tencent": ("tencent_quotes",),
    "tushare": ("tushare_daily",),
    "research": ("score_p0_readiness",),
    "browser": ("browser_refresh",),
    "performance": ("production_performance",),
    "runtime": ("web_health",),
    "sources": ("exchange_security_master", "history_sources", "tencent_quotes", "tushare_daily"),
    "live": ("web_health", "exchange_security_master", "history_sources", "tencent_quotes", "tushare_daily"),
    "full": (
        "web_health",
        "exchange_security_master",
        "history_sources",
        "tencent_quotes",
        "tushare_daily",
        "browser_refresh",
        "production_performance",
    ),
}


@dataclass(frozen=True)
class DiagnosticOptions:
    profile: Profile
    base_url: str
    runtime_config: Path
    codes: tuple[str, ...]
    web_samples: int
    web_interval_seconds: float
    source_samples: int
    source_interval_seconds: float
    history_workers: int
    history_days: int
    web_timeout_seconds: float
    source_timeout_seconds: float
    browser_duration_seconds: float
    browser_minimum_updates: int
    command_timeout_seconds: float
    persistence_runtime_dir: Path | None


@dataclass(frozen=True)
class DiagnosticCommand:
    name: str
    argv: tuple[str, ...]
    timeout_seconds: float


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    return_code: int
    duration_ms: float
    payload: Mapping[str, object] | None
    error_code: str | None


Runner = Callable[[DiagnosticCommand], DiagnosticResult]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=tuple(_PROFILE_CHECKS),
        default="live",
        help="single-check or combined runtime, research, sources, live, and full diagnostic profile",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:5000", help="running trader-server base URL")
    parser.add_argument("--runtime-config", default=str(_DEFAULT_CONFIG), help="runtime JSON configuration")
    parser.add_argument(
        "--codes",
        nargs="+",
        default=("600519", "300750", "688981"),
        help="one to 50 representative six-digit A-share codes",
    )
    parser.add_argument("--web-samples", type=int, default=3, help="Web status/current sample rounds")
    parser.add_argument("--web-interval-seconds", type=float, default=2.0, help="delay between Web samples")
    parser.add_argument("--source-samples", type=int, default=1, help="history and Tencent source sample rounds")
    parser.add_argument("--source-interval-seconds", type=float, default=1.0, help="delay between Tencent samples")
    parser.add_argument("--history-workers", type=int, default=5, help="history requests per bounded worker wave")
    parser.add_argument("--history-days", type=int, default=61, help="daily history rows requested per stock")
    parser.add_argument("--web-timeout-seconds", type=float, default=3.0, help="timeout per Web API request")
    parser.add_argument("--source-timeout-seconds", type=float, default=4.5, help="timeout per vendor HTTP attempt")
    parser.add_argument("--browser-duration-seconds", type=float, default=8.0, help="full-profile browser duration")
    parser.add_argument("--browser-minimum-updates", type=int, default=3, help="required browser DOM updates")
    parser.add_argument(
        "--command-timeout-seconds",
        type=float,
        default=180.0,
        help="hard wall-clock timeout for each child diagnostic",
    )
    parser.add_argument(
        "--persistence-runtime-dir",
        type=Path,
        help="optional absolute repository-external directory for history persistence comparison",
    )
    parser.add_argument("--output", default="-", help="combined JSON output path outside the repository, or -")
    return parser


def _validate(args: argparse.Namespace) -> tuple[DiagnosticOptions, str]:
    codes = tuple(dict.fromkeys(args.codes))
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    if len(codes) > 50:
        raise ValueError("--codes accepts at most 50 codes")
    positive = (
        args.web_samples,
        args.source_samples,
        args.history_workers,
        args.history_days,
        args.web_timeout_seconds,
        args.source_timeout_seconds,
        args.browser_duration_seconds,
        args.browser_minimum_updates,
        args.command_timeout_seconds,
    )
    if any(value <= 0 for value in positive):
        raise ValueError("sample, worker, duration and timeout values must be positive")
    if args.web_interval_seconds < 0 or args.source_interval_seconds < 0:
        raise ValueError("sample intervals must not be negative")
    persistence = _external_path(args.persistence_runtime_dir, "--persistence-runtime-dir")
    output = args.output
    if output != "-":
        output = str(_external_path(Path(output), "--output"))
    return (
        DiagnosticOptions(
            profile=args.profile,
            base_url=args.base_url,
            runtime_config=Path(args.runtime_config).expanduser().resolve(),
            codes=codes,
            web_samples=args.web_samples,
            web_interval_seconds=args.web_interval_seconds,
            source_samples=args.source_samples,
            source_interval_seconds=args.source_interval_seconds,
            history_workers=args.history_workers,
            history_days=args.history_days,
            web_timeout_seconds=args.web_timeout_seconds,
            source_timeout_seconds=args.source_timeout_seconds,
            browser_duration_seconds=args.browser_duration_seconds,
            browser_minimum_updates=args.browser_minimum_updates,
            command_timeout_seconds=args.command_timeout_seconds,
            persistence_runtime_dir=persistence,
        ),
        output,
    )


def _external_path(value: Path | None, option: str) -> Path | None:
    if value is None:
        return None
    if not value.expanduser().is_absolute():
        raise ValueError(f"{option} must be an absolute path outside the repository")
    resolved = value.expanduser().resolve()
    if resolved == PROJECT_ROOT or PROJECT_ROOT in resolved.parents:
        raise ValueError(f"{option} must be an absolute path outside the repository")
    return resolved


def build_commands(
    options: DiagnosticOptions, *, python_executable: str = sys.executable
) -> tuple[DiagnosticCommand, ...]:
    common_timeout = options.command_timeout_seconds
    commands: dict[str, DiagnosticCommand] = {
        "web_health": DiagnosticCommand(
            "web_health",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.web_health",
                "--base-url",
                options.base_url,
                "--samples",
                str(options.web_samples),
                "--interval-seconds",
                str(options.web_interval_seconds),
                "--timeout-seconds",
                str(options.web_timeout_seconds),
                "--consecutive-zero-threshold",
                str(min(3, options.web_samples)),
            ),
            common_timeout,
        ),
        "history_sources": DiagnosticCommand(
            "history_sources",
            _history_command(options, python_executable),
            common_timeout,
        ),
        "exchange_security_master": DiagnosticCommand(
            "exchange_security_master",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.exchange_security_master",
                "--timeout-seconds",
                str(max(15.0, options.source_timeout_seconds)),
            ),
            common_timeout,
        ),
        "tencent_quotes": DiagnosticCommand(
            "tencent_quotes",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.tencent_quotes",
                *options.codes,
                "--samples",
                str(options.source_samples),
                "--interval-seconds",
                str(options.source_interval_seconds),
                "--timeout-seconds",
                str(options.source_timeout_seconds),
            ),
            common_timeout,
        ),
        "tushare_daily": DiagnosticCommand(
            "tushare_daily",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.tushare_daily",
                "--runtime-config",
                str(options.runtime_config),
                "--codes",
                *options.codes,
                "--days",
                str(options.history_days),
            ),
            common_timeout,
        ),
        "score_p0_readiness": DiagnosticCommand(
            "score_p0_readiness",
            (
                python_executable,
                "-m",
                "trader.entrypoints.cli",
                "--config",
                str(options.runtime_config),
                "research-status",
            ),
            common_timeout,
        ),
        "browser_refresh": DiagnosticCommand(
            "browser_refresh",
            (
                python_executable,
                "-m",
                "scripts.runtime_diagnostics.browser_refresh",
                "--duration-seconds",
                str(options.browser_duration_seconds),
                "--minimum-updates",
                str(options.browser_minimum_updates),
                "--runtime-config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),
        "production_performance": DiagnosticCommand(
            "production_performance",
            (
                python_executable,
                "-m",
                "trader.entrypoints.performance",
                "--config",
                str(options.runtime_config),
            ),
            common_timeout,
        ),
    }
    return tuple(commands[name] for name in _PROFILE_CHECKS[options.profile])


def _history_command(options: DiagnosticOptions, python_executable: str) -> tuple[str, ...]:
    command = [
        python_executable,
        "-m",
        "scripts.runtime_diagnostics.history_sources",
        "--codes",
        *options.codes,
        "--samples",
        str(options.source_samples),
        "--workers",
        str(min(options.history_workers, len(options.codes))),
        "--days",
        str(options.history_days),
        "--timeout-seconds",
        str(options.source_timeout_seconds),
    ]
    if options.persistence_runtime_dir is not None:
        command.extend(("--persistence-runtime-dir", str(options.persistence_runtime_dir)))
    return tuple(command)


def execute_command(command: DiagnosticCommand) -> DiagnosticResult:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command.argv,
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=command.timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return DiagnosticResult(command.name, 124, _elapsed_ms(started), None, "command_timeout")
    except OSError:
        return DiagnosticResult(command.name, 126, _elapsed_ms(started), None, "command_launch_failed")
    except UnicodeError:
        return DiagnosticResult(command.name, 1, _elapsed_ms(started), None, "invalid_output_encoding")
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeError, json.JSONDecodeError):
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), None, "invalid_json")
    if not isinstance(decoded, dict) or any(not isinstance(key, str) for key in decoded):
        return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), None, "invalid_json_root")
    return DiagnosticResult(command.name, completed.returncode, _elapsed_ms(started), decoded, None)


def _elapsed_ms(started: float) -> float:
    return round((time.monotonic() - started) * 1000.0, 1)


def run_diagnostics(
    profile: Profile,
    commands: Sequence[DiagnosticCommand],
    *,
    runner: Runner = execute_command,
) -> dict[str, object]:
    return build_report(profile, tuple(runner(command) for command in commands))


def build_report(profile: Profile, results: Sequence[DiagnosticResult]) -> dict[str, object]:
    checks = [_check_payload(result) for result in results]
    statuses = [check["status"] for check in checks]
    summary = {
        "passed": statuses.count("passed"),
        "degraded": statuses.count("degraded"),
        "failed": statuses.count("failed"),
        "total": len(statuses),
    }
    return {
        "schema_version": "trader-runtime-diagnostics-v1",
        "status": "failed" if summary["failed"] else "degraded" if summary["degraded"] else "passed",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "profile": profile,
        "summary": summary,
        "findings": [finding for check in checks for finding in _findings(check)],
        "checks": checks,
    }


def _check_payload(result: DiagnosticResult) -> dict[str, object]:
    status = _status(result)
    payload: dict[str, object] = {
        "name": result.name,
        "status": status,
        "duration_ms": result.duration_ms,
        "schema_version": result.payload.get("schema_version") if result.payload is not None else None,
    }
    if result.error_code is not None:
        payload["error_code"] = result.error_code
    source = result.payload or {}
    if result.name == "web_health":
        payload["summary"] = _mapping(source.get("summary"))
        payload["findings"] = _bounded_findings(source.get("findings"))
        samples = source.get("samples")
        if isinstance(samples, list) and samples and isinstance(samples[-1], dict):
            latest = samples[-1]
            payload["latest_runtime"] = {
                "runtime_status": latest.get("runtime_status"),
                "runtime_version": latest.get("runtime_version"),
                "phase": latest.get("phase"),
                "history_warmup": _mapping(_mapping(latest.get("market")).get("history_warmup")),
                "strategies": _mapping(latest.get("strategies")),
            }
    elif result.name == "history_sources":
        payload["summary"] = _mapping(source.get("summary"))
    elif result.name == "exchange_security_master":
        payload["summary"] = _mapping(source.get("summary")) or {"error": source.get("error")}
    elif result.name == "tencent_quotes":
        versions = _mapping(source.get("distinct_source_versions"))
        payload["summary"] = {
            "latency": _mapping(source.get("latency")),
            "tracked_codes": len(versions),
            "changing_codes": sum(isinstance(value, int) and value > 1 for value in versions.values()),
            "source_changed": source.get("source_changed"),
        }
    elif result.name == "tushare_daily":
        summary = _mapping(source.get("summary"))
        payload["summary"] = {
            "requested_codes": summary.get("requested_codes"),
            "successful_codes": summary.get("successful_codes"),
            "latency_ms": summary.get("latency_ms"),
            "degraded_reason": summary.get("degraded_reason"),
            "capability": _mapping(source.get("capability")),
            "usage": _mapping(source.get("usage")),
        }
    elif result.name == "score_p0_readiness":
        active = _mapping(source.get("active_research"))
        historical = _mapping(active.get("historical_window"))
        blocker = active.get("evaluation_blocker")
        if result.payload is not None and not _valid_research_status(source):
            payload["findings"] = [
                {
                    "severity": "error",
                    "code": "research_status_shape_invalid",
                    "message": "The research status schema or active window projection is invalid.",
                }
            ]
        elif source.get("research_state") == "historical_collection_failed":
            payload["findings"] = [
                {
                    "severity": "error",
                    "code": (
                        blocker
                        if isinstance(blocker, str) and blocker
                        else "score_p0_readiness_historical_collection_failed"
                    ),
                    "message": "The active score research historical window is irrecoverable.",
                    "evidence": {
                        "maximum_attainable_trade_dates": historical.get("maximum_attainable_trade_dates"),
                        "recoverable": historical.get("recoverable"),
                    },
                }
            ]
        payload["summary"] = {
            "research_identity": active.get("research_identity"),
            "historical_window": historical,
            "forward_window": _mapping(active.get("forward_window")),
            "recorded_count": historical.get("recorded_trade_dates"),
            "status": source.get("research_state"),
            "evaluation_blocker": blocker,
        }
    elif result.name == "browser_refresh":
        payload["summary"] = {
            "passed": source.get("passed"),
            "browser_dom": _mapping(_mapping(source.get("browser_dom")).get("summary")),
            "patch_to_paint": _mapping(source.get("browser_patch_to_paint")),
            "decision_patch": _mapping(source.get("browser_decision_patch")),
            "retention": _mapping(source.get("web_snapshot_retention")),
        }
    elif result.name == "production_performance":
        payload["summary"] = {
            "workload": _mapping(source.get("workload")),
            "measurements": _mapping(source.get("measurements")),
            "memory": _mapping(source.get("memory")),
            "failures": _safe_string_list(source.get("failures"), limit=30),
        }
    return payload


def _status(result: DiagnosticResult) -> CheckStatus:
    if result.error_code is not None or result.payload is None or result.return_code != 0:
        return "failed"
    if result.name == "score_p0_readiness" and not _valid_research_status(result.payload):
        return "failed"
    if result.name == "score_p0_readiness" and result.payload.get("research_state") == "historical_collection_failed":
        return "failed"
    status = result.payload.get("status")
    if status == "passed":
        return "passed"
    if status == "degraded":
        return "degraded"
    if status == "failed":
        return "failed"
    passed = result.payload.get("passed")
    if isinstance(passed, bool):
        return "passed" if passed else "failed"
    return "passed"


def _valid_research_status(payload: Mapping[str, object]) -> bool:
    active = payload.get("active_research")
    return (
        payload.get("schema_version") == "v2_research_readiness_v3"
        and isinstance(payload.get("research_state"), str)
        and isinstance(active, dict)
        and isinstance(active.get("research_identity"), str)
        and isinstance(active.get("historical_window"), dict)
        and isinstance(active.get("forward_window"), dict)
    )


def _findings(check: Mapping[str, object]) -> list[dict[str, object]]:
    existing = check.get("findings")
    if isinstance(existing, list):
        return [dict(item, check=check["name"]) for item in existing if isinstance(item, dict)][:50]
    status = check.get("status")
    if status == "passed":
        return []
    return [
        {
            "severity": "error" if status == "failed" else "warning",
            "code": f"{check['name']}_{status}",
            "check": check["name"],
            "evidence": check.get("summary", {"error_code": check.get("error_code")}),
        }
    ]


def _bounded_findings(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)][:50]


def _mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    return {}


def _safe_string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item[:160] for item in value if isinstance(item, str)][:limit]


def _write_report(report: Mapping[str, object], output: str) -> None:
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output == "-":
        sys.stdout.write(rendered)
        return
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        options, output = _validate(args)
        report = run_diagnostics(options.profile, build_commands(options))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        output = args.output if args.output == "-" else "-"
        report = {
            "schema_version": "trader-runtime-diagnostics-v1",
            "status": "failed",
            "error": type(exc).__name__,
        }
    _write_report(report, output)
    return 0 if report.get("status") in {"passed", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
