#!/usr/bin/env python3
"""Build a read-only parent-archive reuse and incremental acquisition plan."""

from __future__ import annotations

import argparse
import io
import json
import sqlite3
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from trader.infra.research.baostock_archive_plan import ArchiveIncrementPlan, build_archive_plan

from .common import emit_report

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_DEFAULT_ARCHIVE = PROJECT_ROOT / "data" / "history" / "baostock-daily" / "sessions-2000"


def _parent_calendar(root: Path) -> tuple[date, tuple[date, ...]]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    relative = manifest["partitions"][0]["relative_path"]
    path = root / relative
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=1", uri=True) as connection:
        row = connection.execute("SELECT spec_json, calendar_json FROM context WHERE singleton=1").fetchone()
    if row is None:
        raise ValueError("parent partition context is missing")
    spec = json.loads(row[0])
    calendar = json.loads(row[1])
    return (
        date.fromisoformat(spec["source_cutoff"]),
        tuple(date.fromisoformat(item) for item in calendar["open_dates"]),
    )


def fetch_open_dates(root: Path, requested_cutoff: date | None) -> tuple[date, ...]:
    """Read the current calendar and append supplier-confirmed complete trade dates."""
    parent_cutoff, parent_calendar = _parent_calendar(root)
    now = datetime.now(_SHANGHAI)
    latest = now.date() if now.time() >= time(15, 30) else now.date() - timedelta(days=1)
    cutoff = min(requested_cutoff, latest) if requested_cutoff is not None else latest
    if cutoff <= parent_cutoff:
        return parent_calendar
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock_dependency_unavailable") from exc
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            login = bs.login()
    except (OSError, RuntimeError, UnboundLocalError) as exc:
        raise RuntimeError("baostock_login_failed") from exc
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        if login.error_code != "0":
            raise RuntimeError("baostock_login_failed")
        try:
            result = bs.query_trade_dates(
                start_date=(parent_cutoff + timedelta(days=1)).isoformat(), end_date=cutoff.isoformat()
            )
            if result.error_code != "0":
                raise RuntimeError("baostock_calendar_query_failed")
            fields = tuple(result.fields)
            extra = []
            while result.next():
                values = tuple(result.get_row_data())
                if len(values) != len(fields):
                    raise ValueError("BaoStock calendar row width is invalid")
                row = dict(zip(fields, values, strict=True))
                if row.get("is_trading_day") == "1":
                    extra.append(date.fromisoformat(row["calendar_date"]))
        finally:
            bs.logout()
    return tuple(sorted(set((*parent_calendar, *extra))))


def _summary(plan: ArchiveIncrementPlan) -> dict[str, object]:
    return {
        "parent_manifest_hash": plan.parent_manifest_hash,
        "parent_manifest_file_hash": plan.parent_manifest_file_hash,
        "parent_logical_records_hash": plan.parent_logical_records_hash,
        "parent_source_cutoff": plan.parent_source_cutoff.isoformat(),
        "latest_complete_trade_date": plan.target_source_cutoff.isoformat(),
        "active_calendar_first_date": plan.active_calendar_dates[0].isoformat(),
        "active_calendar_sessions": len(plan.active_calendar_dates),
        "partition_count": plan.partition_count,
        "universe_count": plan.universe_count,
        "reusable_daily_cells": sum(item.reusable_daily_cells for item in plan.stocks),
        "field_coverage": _json_ready(asdict(plan.field_coverage)),
        "estimated_requests": _json_ready(asdict(plan.estimated_requests) | {"total": plan.estimated_requests.total}),
        "training_input_hash": plan.training_input_hash,
        "training_input_file_hash": plan.training_input_file_hash,
        "production_authority": False,
        "point_in_time_parity": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-root", type=Path, default=_DEFAULT_ARCHIVE)
    parser.add_argument("--target-cutoff", type=date.fromisoformat)
    parser.add_argument("--details-output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        root = args.archive_root.resolve()
        plan = build_archive_plan(root, target_open_dates=fetch_open_dates(root, args.target_cutoff))
        if args.details_output is not None:
            output = args.details_output.resolve()
            if PROJECT_ROOT == output or PROJECT_ROOT in output.parents:
                raise ValueError("details output must be outside the repository")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(_json_ready(asdict(plan)), ensure_ascii=True, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
        report = {
            "schema_version": "history_archive_increment_plan",
            "status": "degraded" if plan.field_coverage.qualification.missing_rows else "passed",
            "summary": _summary(plan),
        }
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError, sqlite3.DatabaseError) as exc:
        report = {"schema_version": "history_archive_increment_plan", "status": "failed", "error": str(exc)}
    emit_report(report)
    return 0 if report["status"] in {"passed", "degraded"} else 1


def _json_ready(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["build_archive_plan", "fetch_open_dates", "main"]
