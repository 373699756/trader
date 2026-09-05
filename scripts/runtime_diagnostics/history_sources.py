#!/usr/bin/env python3
"""Sample live daily-history sources in bounded worker waves and report latency."""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from datetime import time as datetime_time
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from .common import emit_report, summarize_latency_ms

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.application.ports.data_plane import HistoricalFeatureRecord  # noqa: E402
from trader.infra.market_data.history.history import DailyBar  # noqa: E402
from trader.infra.market_data.history.history_seed import FallbackHistoryClient  # noqa: E402
from trader.infra.market_data.providers.eastmoney import EastmoneyClient  # noqa: E402
from trader.infra.market_data.providers.tencent import TencentClient  # noqa: E402
from trader.infra.persistence.data_plane import DataPlaneRepository  # noqa: E402

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_CODES = ("600519", "000001", "300750", "688981", "601318")
Source = Literal["composite", "tencent", "eastmoney"]
TencentHistoryHost = Literal["proxy", "direct"]


@dataclass(frozen=True)
class HistoryObservation:
    sample: int
    code: str
    source: Source
    selected_source: str | None
    row_count: int
    latency_ms: float
    error: str | None
    bars: tuple[DailyBar, ...] = field(repr=False)


@dataclass(frozen=True)
class HistorySamplingOptions:
    samples: int
    workers: int
    source: Source
    days: int
    timeout_seconds: float
    tencent_history_host: TencentHistoryHost = "proxy"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codes", nargs="+", default=_DEFAULT_CODES, help="six-digit A-share codes")
    parser.add_argument("--samples", type=int, default=1, help="worker waves to collect (default: 1)")
    parser.add_argument("--workers", type=int, default=5, help="parallel requests per wave (default: 5)")
    parser.add_argument(
        "--source",
        choices=("composite", "tencent", "eastmoney"),
        default="composite",
        help="production history route to sample",
    )
    parser.add_argument("--days", type=int, default=61, help="daily rows requested per stock (default: 61)")
    parser.add_argument("--timeout-seconds", type=float, default=12.0, help="timeout for each vendor HTTP attempt")
    parser.add_argument(
        "--tencent-history-host",
        choices=("proxy", "direct"),
        default="proxy",
        help="Tencent K-line host; direct is a bounded fallback probe only",
    )
    parser.add_argument(
        "--persistence-runtime-dir",
        help="optional absolute directory outside the repository for an isolated batch-persistence measurement",
    )
    return parser


def _validate(args: argparse.Namespace) -> tuple[str, ...]:
    codes = tuple(dict.fromkeys(args.codes))
    if not codes or any(len(code) != 6 or not code.isdigit() for code in codes):
        raise ValueError("--codes must contain six-digit A-share codes")
    if args.samples < 1 or args.workers < 1 or args.days < 1:
        raise ValueError("--samples, --workers and --days must be positive")
    if args.timeout_seconds <= 0.0:
        raise ValueError("--timeout-seconds must be positive")
    if args.persistence_runtime_dir:
        requested_target = Path(args.persistence_runtime_dir).expanduser()
        target = requested_target.resolve()
        if not requested_target.is_absolute() or target == PROJECT_ROOT or PROJECT_ROOT in target.parents:
            raise ValueError("--persistence-runtime-dir must be an absolute path outside the repository")
    return codes


def _client(options: HistorySamplingOptions):
    if options.source == "tencent":
        return TencentClient(timeout_seconds=options.timeout_seconds)
    if options.source == "eastmoney":
        return EastmoneyClient(timeout_seconds=options.timeout_seconds)
    return FallbackHistoryClient(
        TencentClient(timeout_seconds=options.timeout_seconds),
        EastmoneyClient(timeout_seconds=options.timeout_seconds),
    )


def _sample_one(sample: int, code: str, options: HistorySamplingOptions) -> HistoryObservation:
    started = time.monotonic()
    try:
        client = _client(options)
        if options.source == "tencent":
            bars = tuple(client.fetch_history(code, days=options.days, history_host=options.tencent_history_host))
        else:
            bars = tuple(client.fetch_history(code, days=options.days))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return HistoryObservation(
            sample,
            code,
            options.source,
            None,
            0,
            round((time.monotonic() - started) * 1000.0, 1),
            type(exc).__name__,
            (),
        )
    return HistoryObservation(
        sample,
        code,
        options.source,
        bars[-1].source if bars else None,
        len(bars),
        round((time.monotonic() - started) * 1000.0, 1),
        None,
        bars,
    )


def collect_history_samples(
    codes: tuple[str, ...],
    options: HistorySamplingOptions,
) -> tuple[HistoryObservation, ...]:
    observations: list[HistoryObservation] = []
    with ThreadPoolExecutor(max_workers=min(options.workers, len(codes)), thread_name_prefix="history-sample") as pool:
        for sample in range(1, options.samples + 1):
            futures = {pool.submit(_sample_one, sample, code, options): code for code in codes}
            observations.extend(future.result() for future in as_completed(futures))
    return tuple(sorted(observations, key=lambda item: (item.sample, item.code)))


def _latency_summary(observations: tuple[HistoryObservation, ...]) -> dict[str, float | int | None]:
    return summarize_latency_ms([item.latency_ms for item in observations])


def _measure_persistence(observations: tuple[HistoryObservation, ...], runtime_dir: str) -> dict[str, object]:
    record_batches: list[tuple[HistoricalFeatureRecord, ...]] = []
    for observation in observations:
        if not observation.bars:
            continue
        observed_at = datetime.now(_SHANGHAI)
        version = f"{observation.selected_source}:{observation.bars[-1].trade_date}"
        records = tuple(
            HistoricalFeatureRecord(
                code=observation.code,
                trade_date=bar.trade_date,
                observed_at=observed_at,
                source_time=min(
                    datetime.combine(datetime.fromisoformat(bar.trade_date).date(), datetime_time(15), _SHANGHAI),
                    observed_at,
                ),
                source=observation.selected_source or observation.source,
                data_version=version,
                payload={
                    "trade_date": bar.trade_date,
                    "open_price": bar.open_price,
                    "close": bar.close,
                    "high": bar.high,
                    "low": bar.low,
                    "volume": bar.volume,
                    "amount": bar.amount,
                    "pct_change": bar.pct_change,
                    "turnover_rate": bar.turnover_rate,
                    "adjustment": bar.adjustment.value,
                    "source": bar.source,
                },
            )
            for bar in observation.bars
        )
        record_batches.append(records)
    root = Path(runtime_dir).expanduser().resolve()
    batched = _persist_record_batches(DataPlaneRepository(root / "batched"), record_batches, batched=True)
    single = _persist_record_batches(DataPlaneRepository(root / "single"), record_batches, batched=False)
    batched_latency = float(batched["latency_ms"])
    single_latency = float(single["latency_ms"])
    return {
        "batched": batched,
        "single_record_baseline": single,
        "latency_reduction_ratio": round(single_latency / batched_latency, 2) if batched_latency > 0.0 else None,
    }


def _persist_record_batches(
    repository: DataPlaneRepository,
    record_batches: list[tuple[HistoricalFeatureRecord, ...]],
    *,
    batched: bool,
) -> dict[str, float | int]:
    started = time.monotonic()
    transaction_count = 0
    if batched:
        for records in record_batches:
            repository.save_historical_feature_recent_records(records)
            transaction_count += 1
    else:
        for records in record_batches:
            for record in records:
                repository.save_historical_feature_recent(record)
                transaction_count += 1
    return {
        "record_count": sum(len(records) for records in record_batches),
        "transaction_count": transaction_count,
        "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
    }


def build_report(
    codes: tuple[str, ...],
    observations: tuple[HistoryObservation, ...],
    args: argparse.Namespace,
    persistence: dict[str, object] | None = None,
) -> dict[str, object]:
    usable = sum(item.row_count >= 20 for item in observations)
    return {
        "schema_version": "history-source-sampling",
        "status": "passed" if usable == len(observations) else "degraded",
        "collected_at": datetime.now(_SHANGHAI).isoformat(),
        "configuration": {
            "codes": list(codes),
            "samples": args.samples,
            "workers": min(args.workers, len(codes)),
            "source": args.source,
            "days": args.days,
            "timeout_seconds_per_attempt": args.timeout_seconds,
            "tencent_history_host": args.tencent_history_host,
        },
        "summary": {
            "usable_observations": usable,
            "empty_observations": sum(item.row_count == 0 for item in observations),
            "error_observations": sum(item.error is not None for item in observations),
            "latency": _latency_summary(observations),
            "persistence": persistence,
        },
        "observations": [
            {
                "sample": item.sample,
                "code": item.code,
                "requested_source": item.source,
                "selected_source": item.selected_source,
                "row_count": item.row_count,
                "latency_ms": item.latency_ms,
                "error": item.error,
            }
            for item in observations
        ],
    }


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        codes = _validate(args)
        observations = collect_history_samples(
            codes,
            HistorySamplingOptions(
                samples=args.samples,
                workers=args.workers,
                source=args.source,
                days=args.days,
                timeout_seconds=args.timeout_seconds,
                tencent_history_host=args.tencent_history_host,
            ),
        )
        persistence = (
            _measure_persistence(observations, args.persistence_runtime_dir) if args.persistence_runtime_dir else None
        )
        report = build_report(codes, observations, args, persistence)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "history-source-sampling",
            "status": "failed",
            "error": type(exc).__name__,
        }
    emit_report(report)
    return 0 if report.get("status") in {"passed", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
