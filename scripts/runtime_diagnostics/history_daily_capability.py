#!/usr/bin/env python3
"""Audit the configured daily-history sources without writing an archive."""

from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import date, timedelta
from multiprocessing import get_context
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Literal

from .common import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from trader.infra.research.baostock_session import (  # noqa: E402
    BAOSTOCK_QUERY_INTERVAL_SECONDS,
    BaoStockSessionSdkPort,
    load_baostock_sdk,
    login_baostock,
    logout_baostock,
)
from trader.infra.settings import load_runtime_settings  # noqa: E402

_DAILY_FIELDS = "date,code,adjustflag,tradestatus"
_DEFAULT_CONFIG = PROJECT_ROOT / "config" / "runtime.json"


@dataclass(frozen=True)
class BaoStockDurationEstimate:
    security_count: int
    query_interval_seconds: float
    minimum_single_side_calls: int
    minimum_raw_qfq_calls: int
    minimum_single_side_seconds: float
    minimum_raw_qfq_seconds: float


@dataclass(frozen=True)
class BaoStockProbe:
    status: Literal["passed", "failed"]
    raw_rows: int
    qfq_rows: int
    raw_adjustflag: str | None
    qfq_adjustflag: str | None
    includes_trading_status: bool
    latest_trade_date: str | None
    error: str | None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-config", default=str(_DEFAULT_CONFIG), help="runtime JSON configuration")
    parser.add_argument("--universe-size", type=int, default=5453, help="current stock-universe count")
    parser.add_argument("--code", default="600519", help="one six-digit BaoStock probe code")
    parser.add_argument("--days", type=int, default=120, help="bounded calendar-day probe window")
    parser.add_argument("--timeout-seconds", type=float, default=8.0, help="hard BaoStock probe timeout")
    return parser


def _validate(args: argparse.Namespace) -> None:
    if args.universe_size < 1 or args.days < 1 or args.timeout_seconds <= 0.0:
        raise ValueError("--universe-size, --days and --timeout-seconds must be positive")
    if len(args.code) != 6 or not args.code.isdigit():
        raise ValueError("--code must be a six-digit A-share code")


def minimum_baostock_duration(
    security_count: int,
    *,
    query_interval_seconds: float = BAOSTOCK_QUERY_INTERVAL_SECONDS,
) -> BaoStockDurationEstimate:
    if security_count < 1 or query_interval_seconds <= 0.0:
        raise ValueError("security count and query interval must be positive")
    single_calls = security_count
    raw_qfq_calls = security_count * 2
    return BaoStockDurationEstimate(
        security_count=security_count,
        query_interval_seconds=query_interval_seconds,
        minimum_single_side_calls=single_calls,
        minimum_raw_qfq_calls=raw_qfq_calls,
        minimum_single_side_seconds=(single_calls - 1) * query_interval_seconds,
        minimum_raw_qfq_seconds=(raw_qfq_calls - 1) * query_interval_seconds,
    )


def probe_baostock(
    code: str,
    *,
    days: int,
    timeout_seconds: float,
    today: date | None = None,
) -> BaoStockProbe:
    context = get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_probe_worker, args=(child, code, days, today), daemon=True)
    try:
        process.start()
    except (OSError, RuntimeError):
        parent.close()
        child.close()
        raise
    child.close()
    try:
        if not parent.poll(timeout_seconds):
            process.terminate()
            process.join(timeout=1.0)
            return BaoStockProbe("failed", 0, 0, None, None, False, None, "supplier_call_timeout")
        result = parent.recv()
        if isinstance(result, BaoStockProbe):
            return result
        return BaoStockProbe("failed", 0, 0, None, None, False, None, "probe_protocol_invalid")
    except (EOFError, OSError):
        return BaoStockProbe("failed", 0, 0, None, None, False, None, "probe_process_failed")
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(timeout=1.0)


def _probe_worker(connection: Connection, code: str, days: int, today: date | None) -> None:
    try:
        connection.send(_probe_baostock_in_process(code, days=days, today=today))
    except (EOFError, OSError):
        pass
    finally:
        connection.close()


def _probe_baostock_in_process(code: str, *, days: int, today: date | None = None) -> BaoStockProbe:
    source_code = f"sh.{code}" if code.startswith(("5", "6", "9")) else f"sz.{code}"
    end = today or date.today()
    start = end - timedelta(days=days)
    sdk = None
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            sdk = load_baostock_sdk()
            login_baostock(sdk)
            raw = _query_summary(sdk, source_code, start, end, adjustflag="3")
            qfq = _query_summary(sdk, source_code, start, end, adjustflag="2")
    except Exception as exc:  # third-party SDK raises inconsistent exception types
        return BaoStockProbe("failed", 0, 0, None, None, False, None, _safe_error(exc))
    finally:
        if sdk is not None:
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                logout_baostock(sdk)
    raw_count, raw_flag, includes_status, latest = raw
    qfq_count, qfq_flag, _, _ = qfq
    passed = raw_count > 0 and qfq_count > 0 and raw_flag == "3" and qfq_flag == "2" and includes_status
    return BaoStockProbe(
        "passed" if passed else "failed",
        raw_count,
        qfq_count,
        raw_flag,
        qfq_flag,
        includes_status,
        latest,
        None if passed else "raw_qfq_probe_incomplete",
    )


def _query_summary(
    sdk: BaoStockSessionSdkPort,
    source_code: str,
    start: date,
    end: date,
    *,
    adjustflag: str,
) -> tuple[int, str | None, bool, str | None]:
    result = sdk.query_history_k_data_plus(
        source_code,
        _DAILY_FIELDS,
        start.isoformat(),
        end.isoformat(),
        frequency="d",
        adjustflag=adjustflag,
    )
    if str(result.error_code) != "0":
        raise RuntimeError(_supplier_error(str(result.error_code)))
    fields = tuple(str(item) for item in result.fields)
    positions = {field: index for index, field in enumerate(fields)}
    count = 0
    observed_flags: set[str] = set()
    latest: str | None = None
    while str(result.error_code) == "0" and result.next():
        row = tuple(str(item) for item in result.get_row_data())
        count += 1
        if "adjustflag" in positions:
            observed_flags.add(row[positions["adjustflag"]])
        if "date" in positions:
            value = row[positions["date"]]
            latest = value if latest is None or value > latest else latest
    if str(result.error_code) != "0":
        raise RuntimeError(_supplier_error(str(result.error_code)))
    observed_flag = next(iter(observed_flags)) if len(observed_flags) == 1 else None
    return count, observed_flag, "tradestatus" in positions, latest


def _supplier_error(error_code: str) -> str:
    if error_code == "10001011":
        return "supplier_query_failed_blacklisted"
    return "supplier_query_rejected"


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    if isinstance(exc, RuntimeError) and message.startswith("supplier_"):
        return message[:80]
    return type(exc).__name__


def build_report(
    args: argparse.Namespace,
    *,
    tushare_points: int,
    tushare_enabled: bool,
    baostock: BaoStockProbe,
) -> dict[str, object]:
    estimate = minimum_baostock_duration(args.universe_size)
    tushare_raw = tushare_enabled and tushare_points >= 120
    tushare_adjustment = tushare_enabled and tushare_points >= 2000
    blockers = []
    if not tushare_enabled:
        blockers.append("configured_tushare_unavailable")
    elif not tushare_adjustment:
        blockers.append("configured_tushare_lacks_adjustment_factor_access")
    else:
        blockers.append("tushare_code_change_and_complete_cutoff_not_validated")
    blockers.extend(
        (
            "baostock_requires_per_security_raw_and_qfq_queries",
            "tencent_and_eastmoney_have_no_validated_market_day_batch_contract",
        )
    )
    raw_qfq_observed = (
        baostock.status == "passed"
        and baostock.raw_adjustflag == "3"
        and baostock.qfq_adjustflag == "2"
        and baostock.raw_rows > 0
        and baostock.qfq_rows > 0
    )
    return {
        "schema_version": "history-daily-capability-audit",
        "status": "degraded",
        "configuration": {
            "universe_size": args.universe_size,
        },
        "decision": {
            "status": "blocked",
            "selected_baseline_source": "baostock",
            "efficient_daily_source": None,
            "blockers": blockers,
        },
        "baostock_lower_bound": {
            "query_interval_seconds": estimate.query_interval_seconds,
            "minimum_single_side_calls": estimate.minimum_single_side_calls,
            "minimum_raw_qfq_calls": estimate.minimum_raw_qfq_calls,
            "minimum_single_side_seconds": estimate.minimum_single_side_seconds,
            "minimum_raw_qfq_seconds": estimate.minimum_raw_qfq_seconds,
        },
        "candidates": [
            {
                "source": "baostock",
                "request_scope": "security_range",
                "market_day_batch": False,
                "raw_qfq_semantics_observed": raw_qfq_observed,
                "trading_status_field_observed": baostock.includes_trading_status,
                "code_change_contract": False,
                "complete_cutoff_contract": False,
                "latest_observed_trade_date": baostock.latest_trade_date,
                "probe_status": baostock.status,
                "probe_error": baostock.error,
            },
            {
                "source": "tushare",
                "request_scope": "market_day",
                "access_points": tushare_points,
                "market_day_raw": tushare_raw,
                "adjustment_factor": tushare_adjustment,
                "suspended_rows": False,
                "code_change_contract": False,
                "complete_cutoff_contract": False,
            },
            {
                "source": "tencent_eastmoney",
                "request_scope": "security_range",
                "market_day_batch": False,
                "raw_qfq_pair": True,
                "trading_status_contract": False,
                "code_change_contract": False,
                "complete_cutoff_contract": False,
            },
        ],
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        _validate(args)
        settings = load_runtime_settings(Path(args.runtime_config).expanduser().resolve())
        report = build_report(
            args,
            tushare_points=settings.market_data.tushare.points,
            tushare_enabled=settings.market_data.tushare.enabled,
            baostock=probe_baostock(args.code, days=args.days, timeout_seconds=args.timeout_seconds),
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        report = {
            "schema_version": "history-daily-capability-audit",
            "status": "failed",
            "error": type(exc).__name__,
        }
    emit_report(report)
    return 0 if report.get("status") in {"passed", "degraded"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
