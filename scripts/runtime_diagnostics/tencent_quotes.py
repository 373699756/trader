#!/usr/bin/env python3
"""Sample Tencent quotes repeatedly and report source-version stability and request latency."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .common import emit_report, summarize_latency_ms

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.infra.market_data.providers.tencent import TencentClient  # noqa: E402

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", default=("000001", "600519"), help="six-digit A-share codes")
    parser.add_argument("--samples", type=int, default=5, help="number of samples (default: 5)")
    parser.add_argument("--interval-seconds", type=float, default=2.0, help="wall-clock delay between samples")
    parser.add_argument("--timeout-seconds", type=float, default=3.0, help="timeout for each Tencent HTTP request")
    return parser


def _latency_summary(values: list[float]) -> dict[str, float | int | None]:
    return summarize_latency_ms(values)


def _validate(args: argparse.Namespace) -> tuple[str, ...]:
    codes = tuple(sorted(set(args.codes)))
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("codes must be six-digit A-share codes")
    if args.samples < 1:
        raise ValueError("--samples must be positive")
    if args.interval_seconds < 0.0:
        raise ValueError("--interval-seconds must not be negative")
    if args.timeout_seconds <= 0.0:
        raise ValueError("--timeout-seconds must be positive")
    return codes


def _collect(codes: tuple[str, ...], args: argparse.Namespace) -> dict[str, object]:
    client = TencentClient(timeout_seconds=args.timeout_seconds)
    samples: list[dict[str, object]] = []
    latencies: list[float] = []
    versions: dict[str, list[str]] = {code: [] for code in codes}
    for sample_number in range(1, args.samples + 1):
        started = time.monotonic()
        quotes = client.fetch_quotes(codes)
        latency_ms = (time.monotonic() - started) * 1000.0
        latencies.append(latency_ms)
        quote_rows: list[dict[str, object]] = []
        for quote in quotes:
            versions.setdefault(quote.code, []).append(quote.data_version)
            quote_rows.append(
                {
                    "code": quote.code,
                    "price": quote.price,
                    "source_time": quote.source_time.isoformat(),
                    "received_time": quote.received_time.isoformat(),
                    "data_version": quote.data_version,
                }
            )
        samples.append(
            {
                "sample": sample_number,
                "latency_ms": round(latency_ms, 1),
                "quotes": quote_rows,
            }
        )
        if sample_number < args.samples:
            time.sleep(args.interval_seconds)
    distinct_versions = {code: len(set(values)) for code, values in sorted(versions.items())}
    return {
        "schema_version": "tencent-quote-sampling-v1",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "codes": list(codes),
        "requested_interval_seconds": args.interval_seconds,
        "latency": _latency_summary(latencies),
        "distinct_source_versions": distinct_versions,
        "source_changed": any(count > 1 for count in distinct_versions.values()),
        "samples": samples,
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    report: dict[str, object]
    try:
        codes = _validate(args)
        report = _collect(codes, args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "tencent-quote-sampling-v1",
            "status": "failed",
            "error": type(exc).__name__,
        }
        emit_report(report)
        return 1
    emit_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
