#!/usr/bin/env python3
"""Probe the official SSE/SZSE security-master snapshot and report bounded coverage counts."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.infra.market_data.providers.exchange_security_master import ExchangeSecurityMasterClient  # noqa: E402

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout-seconds", type=float, default=15.0, help="timeout for each official request")
    parser.add_argument(
        "--minimum-rows",
        type=int,
        default=4_000,
        help="minimum complete supported A-share rows required",
    )
    return parser


def collect(client: ExchangeSecurityMasterClient, observed_at: datetime) -> dict[str, object]:
    observations = client.fetch(observed_at)
    exchange_rows = Counter(str(item.fields["exchange"]) for item in observations)
    board_rows = Counter(str(item.fields["board"]) for item in observations)
    listing_date_rows = sum(isinstance(item.fields.get("listing_date"), str) for item in observations)
    total_rows = len(observations)
    health = client.health()
    return {
        "schema_version": "exchange-security-master-probe-v1",
        "status": "passed",
        "collected_at": observed_at.isoformat(),
        "summary": {
            "total_rows": total_rows,
            "listing_date_rows": listing_date_rows,
            "coverage_ratio": round(listing_date_rows / total_rows, 6) if total_rows else 0.0,
            "exchange_rows": dict(sorted(exchange_rows.items())),
            "board_rows": dict(sorted(board_rows.items())),
        },
        "latency_ms": health.last_latency_ms,
        "data_version_count": len({item.data_version for item in observations}),
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.timeout_seconds <= 0.0 or args.minimum_rows <= 0:
        parser.error("timeout and minimum rows must be positive")
    observed_at = datetime.now(_SHANGHAI)
    client = ExchangeSecurityMasterClient(
        timeout_seconds=args.timeout_seconds,
        minimum_rows=args.minimum_rows,
        wall_clock=lambda: datetime.now(_SHANGHAI),
    )
    try:
        report = collect(client, observed_at)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "exchange-security-master-probe-v1",
            "status": "failed",
            "collected_at": observed_at.isoformat(),
            "error": client.health().last_error or type(exc).__name__,
        }
        emit_report(report)
        return 1
    emit_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
