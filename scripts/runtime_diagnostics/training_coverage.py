"""Read-only coverage audit, including caches produced before persisted sample statistics."""

from __future__ import annotations

import argparse
import math
from collections import Counter
from datetime import date
from pathlib import Path

from trader.infra.settings import LoadRuntimeSettings
from trader.infra.sqlite_connection import SqliteConnection, SqliteConnectionOptions, SqliteSession
from trader.training.domain.label_benchmark import TRAINING_LABEL_BENCHMARK_CONTRACT
from trader.training.infra.history.history_training_input import SQLiteHistoryTrainingInputSnapshot

from .reporting import emit_report


def _audit(path: Path) -> dict[str, object]:
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=10, cache_kib=8192)) as connection:
        connection.run("PRAGMA query_only=ON")
        connection.run("PRAGMA cache_size=-8192")
        tables = {str(row[0]) for row in connection.run("SELECT name FROM sqlite_master WHERE type='table'")}
        if {"sample_statistics", "sample_day_diagnostics"}.issubset(tables):
            return _audit_statistics(connection)
        days = connection.run("SELECT trade_date,split_name FROM sample_split_dates ORDER BY trade_date").fetchall()
        missing: list[dict[str, object]] = []
        for day, partition in days:
            present = connection.run(
                "SELECT target_t1,target_d25_aggregate FROM samples WHERE trade_date=? LIMIT 1", (day,)
            ).fetchone()
            if present is not None and all(value is not None for value in present):
                continue
            counts = connection.run(
                "SELECT COUNT(*),COUNT(target_t1),COUNT(target_d25_aggregate) FROM samples WHERE trade_date=?",
                (day,),
            ).require_one()
            if counts[1] == 0 or counts[2] == 0:
                missing.append(
                    {
                        "date": day,
                        "partition": partition,
                        "sample_rows": counts[0],
                        "t1_rows": counts[1],
                        "d25_rows": counts[2],
                    }
                )
        return {"expected_dates": len(days), "missing": missing}


def _audit_statistics(connection: SqliteConnection) -> dict[str, object]:
    rows = connection.run(
        "SELECT d.trade_date,d.split_name,"
        "COALESCE(SUM(CASE WHEN s.target='rows' THEN s.rows END),0),"
        "COALESCE(SUM(CASE WHEN s.target='target_t1' THEN s.rows END),0),"
        "COALESCE(SUM(CASE WHEN s.target='target_d25_aggregate' THEN s.rows END),0) "
        "FROM sample_split_dates d LEFT JOIN sample_statistics s ON s.trade_date=d.trade_date "
        "GROUP BY d.trade_date ORDER BY d.trade_date"
    ).fetchall()
    missing = [
        {"date": day, "partition": partition, "sample_rows": samples, "t1_rows": t1, "d25_rows": d25}
        for day, partition, samples, t1, d25 in rows
        if not t1 or not d25
    ]
    projected = []
    for partition, expected, diagnosed, t1_dates, d25_dates, t1_rows, d25_rows in connection.run(
        "SELECT d.split_name,COUNT(*),COUNT(g.trade_date),"
        "SUM(CASE WHEN g.candidates>g.missing_t1 THEN 1 ELSE 0 END),"
        "SUM(CASE WHEN g.candidates>g.missing_t2+g.missing_t3+g.missing_t4+g.missing_t5 THEN 1 ELSE 0 END),"
        "SUM(MAX(0,COALESCE(g.candidates-g.missing_t1,0))),"
        "SUM(MAX(0,COALESCE(g.candidates-g.missing_t2-g.missing_t3-g.missing_t4-g.missing_t5,0))) "
        "FROM sample_split_dates d LEFT JOIN sample_day_diagnostics g ON g.trade_date=d.trade_date "
        "GROUP BY d.split_name ORDER BY MIN(d.trade_date)"
    ):
        projected.append(
            {
                "partition": partition,
                "expected_dates": expected,
                "diagnosed_dates": diagnosed,
                "effective_t1_dates": t1_dates,
                "minimum_effective_d25_dates": d25_dates,
                "eligible_t1_rows": t1_rows,
                "minimum_eligible_d25_rows": d25_rows,
            }
        )
    return {
        "expected_dates": len(rows),
        "missing": missing,
        "observed_label_contract": TRAINING_LABEL_BENCHMARK_CONTRACT,
        "observed_label_projection": projected,
        "projection_is_training_result": False,
    }


def _trace_labels(history: Path, path: Path, signal: date) -> dict[str, object]:
    archive = SQLiteHistoryTrainingInputSnapshot.open(history)
    calendar = archive.snapshot.calendar.open_dates
    position = calendar.index(signal)
    future = calendar[position + 1 : position + 6]
    if len(future) != 5:
        raise ValueError("training_label_probe_requires_mature_signal")
    with SqliteSession(path, SqliteConnectionOptions(read_only=True, timeout_seconds=10, cache_kib=8192)) as connection:
        codes = frozenset(
            str(row[0])
            for row in connection.run(
                "SELECT code FROM samples WHERE trade_date=? AND target_t1 IS NOT NULL", (signal.isoformat(),)
            )
        )
    if not codes:
        raise ValueError("training_label_probe_requires_observed_t1_population")
    records = tuple(row for row in archive.iter_training_rows(frozenset(future)) if row.code in codes)
    horizons = []
    for horizon, day in enumerate(future, 1):
        rows = tuple(row for row in records if row.trade_date == day)
        unavailable = tuple(
            row
            for row in rows
            if row.cell_status not in {None, "complete", "supplier_marked_suspended"}
            or row.point is None
            or row.point.qfq_close_price is None
            or not math.isfinite(row.point.qfq_close_price)
            or row.point.qfq_close_price <= 0
        )
        horizons.append(
            {
                "horizon": horizon,
                "date": day.isoformat(),
                "population": len(codes),
                "absent_rows": len(codes) - len(rows),
                "unusable_source_rows": len(unavailable),
                "source_statuses": dict(Counter(row.cell_status or "complete" for row in unavailable)),
            }
        )
    return {
        "signal_date": signal.isoformat(),
        "population": len(codes),
        "horizons": horizons,
        "source_data_writes": 0,
        "price_scan_passes": 1,
        "interpretation": "observed_t1_sample_population; observed_returns_only_per_horizon; estimates_cannot_supply_labels",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--signal-date", type=date.fromisoformat)
    args = parser.parse_args()
    paths = LoadRuntimeSettings(args.config).data_paths
    if args.signal_date is not None:
        emit_report(
            {
                "schema_version": "training-coverage-diagnostic",
                "status": "degraded",
                "summary": _trace_labels(paths.history, paths.samples / "v5/samples.sqlite3", args.signal_date),
            }
        )
        return 0
    profiles = []
    for profile in ("v2", "v3", "v5"):
        path = paths.samples / profile / "samples.sqlite3"
        if path.is_file():
            profiles.append({"profile": profile, **_audit(path)})
    emit_report(
        {
            "schema_version": "training-coverage-diagnostic",
            "status": "degraded" if not profiles or any(item["missing"] for item in profiles) else "passed",
            "summary": {"profiles": profiles, "source_data_writes": 0},
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
