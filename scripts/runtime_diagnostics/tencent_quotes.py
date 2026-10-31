#!/usr/bin/env python3
"""Sample Tencent quotes repeatedly and report source-version stability and request latency."""

from __future__ import annotations

from trader.infra.sqlite_policy import SqliteIoPolicy

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .reporting import emit_report, summarize_latency_ms

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.infra.http_contracts import HttpAttempt, HttpError, HttpTransport  # noqa: E402
from trader.infra.http_transport import RequestsHttpTransport  # noqa: E402
from trader.infra.isolated_http import IsolatedHttpExecutor  # noqa: E402
from trader.infra.workers import BoundedExecutor  # noqa: E402
from trader.recommendation.domain.market.models import MarketQuote  # noqa: E402
from trader.recommendation.infra.market_data.providers.tencent import TencentClient  # noqa: E402

_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("codes", nargs="*", default=("000001", "600519"), help="six-digit A-share codes")
    parser.add_argument("--samples", type=int, default=5, help="number of samples (default: 5)")
    parser.add_argument("--interval-seconds", type=float, default=2.0, help="wall-clock delay between samples")
    parser.add_argument("--timeout-seconds", type=float, default=3.0, help="timeout for each Tencent HTTP request")
    parser.add_argument("--transport-audit", action="store_true")
    parser.add_argument("--runtime-config", type=Path, default=PROJECT_ROOT / "config/runtime.json")
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
    from trader.bootstrap import OpenExternalHttp

    client = TencentClient(worker_pool=None, session_factory=OpenExternalHttp, timeout_seconds=args.timeout_seconds)
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
        "schema_version": "tencent-quote-sampling",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "codes": list(codes),
        "requested_interval_seconds": args.interval_seconds,
        "latency": _latency_summary(latencies),
        "distinct_source_versions": distinct_versions,
        "source_changed": any(count > 1 for count in distinct_versions.values()),
        "samples": samples,
    }


def _audit_transport(args: argparse.Namespace, *, sqlite_policy: SqliteIoPolicy) -> dict[str, object]:
    from trader.infra.settings import LoadRuntimeSettings
    from .market_factor_inputs import ReadReferences

    settings = LoadRuntimeSettings(args.runtime_config)
    references = ReadReferences(settings.data_paths.reference, datetime.now(_SHANGHAI), sqlite_policy=sqlite_policy)
    codes = tuple(sorted({row.subject_key for row in references.observations}))
    rows: list[dict[str, object]] = []
    pool = BoundedExecutor(worker_count=3, queue_capacity=3, thread_name_prefix="tencent-audit")
    pool.start()
    executor = IsolatedHttpExecutor(worker_count=3, transport_factory=RequestsHttpTransport)
    executor.start()
    try:
        for isolated in (False, True):
            attempts: list[HttpAttempt] = []

            def open_transport(*, attempts: list[HttpAttempt] = attempts, isolated: bool = isolated) -> HttpTransport:
                return RequestsHttpTransport(
                    observe=attempts.append,
                    execute_request=executor.execute if isolated else None,
                )

            client = TencentClient(worker_pool=pool, session_factory=open_transport,
                                   timeout_seconds=args.timeout_seconds)
            started = time.monotonic()
            error = ""
            quotes: tuple[MarketQuote, ...] = ()
            try:
                quotes = client.fetch_market(
                    codes,
                    deadline=datetime.now(_SHANGHAI)
                    + timedelta(seconds=settings.market_data.full_market_primary_timeout_seconds),
                    request_attempts=settings.market_data.full_market_quote_attempts,
                )
            except (OSError, RuntimeError, HttpError) as exc:
                error = type(exc).__name__
            rows.append({
                "transport": "isolated" if isolated else "direct", "requested": len(codes),
                "returned": len(quotes), "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                "attempts": len(attempts), "failed_attempts": sum(bool(item.failure) for item in attempts),
                "maximum_shard_attempts": settings.market_data.full_market_quote_attempts,
                "attempt_latency": _latency_summary([item.elapsed_seconds * 1000 for item in attempts]),
                "error": error,
            })
    finally:
        pool.stop(wait=True, cancel_futures=True)
        executor.stop()
    return {
        "schema_version": "tencent-transport-audit",
        "status": "degraded" if any(row["error"] or row["returned"] != row["requested"] for row in rows) else "passed",
        "summary": {"measurements": rows},
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    from trader.bootstrap import BuildSqlitePolicy
    from trader.infra.settings import LoadRuntimeSettings
    sqlite_policy = BuildSqlitePolicy(LoadRuntimeSettings(args.runtime_config))
    report: dict[str, object]
    try:
        codes = _validate(args)
        report = _audit_transport(args, sqlite_policy=sqlite_policy) if args.transport_audit else _collect(codes, args)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "tencent-quote-sampling",
            "status": "failed",
            "error": type(exc).__name__,
        }
        emit_report(report)
        return 1
    emit_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
