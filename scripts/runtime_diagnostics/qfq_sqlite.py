"""Compare legacy and board-routed qfq SQLite using an isolated, verified migration."""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import statistics
import time
from collections import defaultdict
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory

from trader.download.infra.qfq_layout_migration import build_qfq_layout, inspect_legacy_qfq, legacy_windows
from trader.download.infra.qfq_sqlite import QFQ_SHARD_NAMES, SQLiteQfqWindowCache, qfq_shard_name

from .reporting import emit_report


def _query(root, routing, codes):
    grouped = defaultdict(list)
    for code in codes:
        grouped[routing[code]].append(code)
    digests = []
    rows = 0
    started = time.monotonic()
    for name, requested in sorted(grouped.items()):
        path = root / name
        with (
            closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)) as connection,
            connection,
        ):
            connection.execute("BEGIN")
            for code in sorted(requested):
                digest = hashlib.sha256()
                for day, payload in connection.execute(
                    "SELECT day,payload FROM bars WHERE code=? ORDER BY day", (code,)
                ):
                    digest.update(f"{code}:{day}:{payload}".encode("ascii"))
                    rows += 1
                digests.append((code, digest.hexdigest()))
    return time.monotonic() - started, rows, tuple(sorted(digests)), len(grouped)


def _comparison(profile, target, codes, rounds):
    old_routing = dict(profile.membership)
    new_routing = {code: qfq_shard_name(code) for code in codes}
    old_times = []
    new_times = []
    for round_number in range(rounds):
        # Alternate order; both are process-local repeated reads, not OS cold-cache measurements.
        ordered = ((profile.root, old_routing), (target, new_routing))
        if round_number % 2:
            ordered = tuple(reversed(ordered))
        values = [_query(root, routing, codes) for root, routing in ordered]
        old, new = values if round_number % 2 == 0 else tuple(reversed(values))
        if old[1:3] != new[1:3]:
            raise ValueError("qfq benchmark row equality failure")
        old_times.append(old[0])
        new_times.append(new[0])
    return {
        "stocks": len(codes),
        "rows": new[1],
        "legacy_connections": old[3],
        "board_connections": new[3],
        "legacy_median_seconds": round(statistics.median(old_times), 6),
        "board_median_seconds": round(statistics.median(new_times), 6),
        "rounds": rounds,
    }


def _writes(profile, target):
    cache = SQLiteQfqWindowCache(target, profile.profile)
    samples = tuple(legacy_windows(profile, tuple(code for code, _ in profile.membership[:20])))
    before = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in cache.root.glob("*.sqlite3")}
    started = time.monotonic()
    for window, source in samples:
        if cache.replace_window(window, source) != ((), 0):
            raise ValueError("qfq no-op wrote files")
    no_op = time.monotonic() - started
    after = {path.name: (path.stat().st_mtime_ns, path.stat().st_size) for path in cache.root.glob("*.sqlite3")}
    if before != after:
        raise ValueError("qfq no-op changed file identities")
    changed_rows = 0
    started = time.monotonic()
    for window, source in samples:
        cell = window.cells[-1]
        revised = replace(cell, unadjusted=_revise_side(cell.unadjusted), qfq=_revise_side(cell.qfq))
        updated = replace(window, cells=(*window.cells[:-1], revised))
        _, count = cache.replace_window(updated, source)
        changed_rows += count
    return {
        "stocks": len(samples),
        "no_op_seconds": round(no_op, 6),
        "no_op_changed_files": 0,
        "one_row_revision_seconds": round(time.monotonic() - started, 6),
        "revision_changed_rows": changed_rows,
    }


def _revise_side(side):
    if side is None:
        return None
    return replace(
        side,
        open_price=None if side.open_price is None else side.open_price + 0.01,
        high_price=None if side.high_price is None else side.high_price + 0.01,
        low_price=None if side.low_price is None else side.low_price + 0.01,
        close_price=None if side.close_price is None else side.close_price + 0.01,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qfq-root", type=Path, required=True)
    parser.add_argument("--rounds", type=int, default=3)
    args = parser.parse_args()
    try:
        if not 1 <= args.rounds <= 9:
            raise ValueError("qfq query rounds outside 1..9")
        with TemporaryDirectory(prefix="trader-qfq-layout-") as scratch:
            root = Path(scratch)
            target = root / "qfq"
            started = time.monotonic()
            migration = build_qfq_layout(args.qfq_root, target, root / ".lock")
            elapsed = time.monotonic() - started
            profiles = inspect_legacy_qfq(args.qfq_root)
            workloads = []
            writes = []
            for profile in profiles:
                codes = tuple(code for code, _ in profile.membership)
                sample = tuple(
                    codes[index * len(codes) // min(100, len(codes))] for index in range(min(100, len(codes)))
                )
                for label, requested in (("single", sample[:1]), ("batch100", sample), ("full", codes)):
                    workloads.append(
                        {
                            "profile": profile.profile,
                            "workload": label,
                            **_comparison(profile, target / profile.profile, requested, args.rounds),
                        }
                    )
                writes.append({"profile": profile.profile, **_writes(profile, target)})
            plans = []
            for path in target.rglob("*.sqlite3"):
                with closing(sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)) as connection:
                    plan = connection.execute(
                        "EXPLAIN QUERY PLAN SELECT day,payload FROM bars WHERE code=? ORDER BY day", ("000001",)
                    ).fetchall()
                    plans.append("PRIMARY KEY" in str(plan) and "TEMP B-TREE" not in str(plan))
            report = {
                "schema_version": "qfq-sqlite-diagnostic",
                "status": "passed" if all(plans) else "failed",
                "summary": {
                    "migration": asdict(migration),
                    "migration_seconds": round(elapsed, 4),
                    "source_unchanged": True,
                    "active_data_switched": False,
                    "cache_condition": "alternating_process_local_repeated_reads",
                    "query_scope": "identical_SQL_and_payload_hashing_without_domain_decoding",
                    "queries": workloads,
                    "writes_in_isolated_target": writes,
                    "primary_key_plans": all(plans),
                    "max_partitions_per_profile": len(QFQ_SHARD_NAMES),
                },
            }
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        report = {"schema_version": "qfq-sqlite-diagnostic", "status": "failed", "error": type(exc).__name__}
    emit_report(report)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
