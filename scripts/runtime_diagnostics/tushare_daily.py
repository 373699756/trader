#!/usr/bin/env python3
"""Probe the configured Tushare daily capability without exposing credentials or prices."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.infra.market_data.tushare import TushareClient  # noqa: E402
from trader.infra.settings import load_runtime_settings  # noqa: E402

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_RUNTIME_CONFIG = PROJECT_ROOT / "config" / "v2" / "runtime.json"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", default=str(_DEFAULT_RUNTIME_CONFIG), help="runtime JSON configuration")
    parser.add_argument("--codes", nargs="+", default=("000001",), help="one to 50 six-digit A-share codes")
    parser.add_argument("--days", type=int, default=61, help="minimum calendar lookback days (default: 61)")
    return parser


def _validate(args: argparse.Namespace) -> tuple[str, ...]:
    codes = tuple(dict.fromkeys(args.codes))
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    if len(codes) > 50:
        raise ValueError("--codes accepts at most 50 codes per 120-point minute quota")
    if args.days < 1:
        raise ValueError("--days must be positive")
    return codes


def _report(args: argparse.Namespace, codes: tuple[str, ...]) -> dict[str, object]:
    settings = load_runtime_settings(Path(args.runtime_config).expanduser().resolve())
    configured = settings.market_data.tushare
    now = datetime.now(_SHANGHAI)
    client = TushareClient(
        token=configured.token if configured.enabled else "",
        points=configured.points,
        timeout_seconds=configured.timeout_seconds,
        wall_clock=lambda: datetime.now(_SHANGHAI),
    )
    started = time.monotonic()
    observations = client.fetch_daily_history(
        codes,
        (now - timedelta(days=max(args.days * 2, args.days + 14))).date(),
        now.date(),
        now,
    )
    latency_ms = round((time.monotonic() - started) * 1000.0, 1)
    successful = tuple(item for item in observations if item.status == "success")
    health = client.health()
    row_counts = {code: sum(item.subject_key == code for item in successful) for code in codes}
    all_raw = bool(successful) and all(item.fields.get("price_adjustment") == "raw" for item in successful)
    return {
        "schema_version": "tushare-daily-sampling-v1",
        "status": "passed" if all(row_counts.values()) and all_raw else "degraded",
        "collected_at": now.isoformat(),
        "configuration": {"codes": list(codes), "days": args.days, "timeout_seconds": configured.timeout_seconds},
        "capability": {
            "access_points": health.access_points,
            "history_mode": health.history_mode,
            "minute_call_limit": health.minute_call_limit,
            "daily_call_limit": health.daily_call_limit,
            "forward_adjusted_daily": client.supports("forward_adjusted_daily"),
            "price_adjustment": "raw" if all_raw else None,
        },
        "usage": {
            "process_api_attempts_last_minute": health.process_api_attempts_last_minute,
            "process_api_attempts_today": health.process_api_attempts_today,
            "process_remaining_calls_today": health.process_remaining_calls_today,
            "local_rate_limit_count": health.local_rate_limit_count,
        },
        "summary": {
            "requested_codes": len(codes),
            "successful_codes": sum(count > 0 for count in row_counts.values()),
            "row_counts": row_counts,
            "latency_ms": latency_ms,
            "degraded_reason": health.degraded_reason,
        },
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        report = _report(args, _validate(args))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "tushare-daily-sampling-v1",
            "status": "failed",
            "error": type(exc).__name__,
        }
    emit_report(report)
    return 0 if report.get("status") in {"passed", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
