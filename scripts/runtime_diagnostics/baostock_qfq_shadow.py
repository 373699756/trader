"""Read-only BaoStock qfq reuse shadow report for the active history snapshot."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from trader.download.infra.history_archive_reader import SQLiteHistoryArchiveReader
from trader.download.infra.history_archive_status import HistoryArchiveError, load_active_history_archive

from .reporting import emit_report

_V3_SESSIONS = 61
_V2_SESSIONS = 251
_RECENT_REFRESH_SESSIONS = 5


@dataclass
class _CodeFacts:
    expected: set[date]
    observed: set[date]
    raw_dates: set[date]
    qfq_dates: set[date]
    duplicate_rows: int = 0
    invalid_rows: int = 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history-root", type=Path, required=True)
    return parser


def build_shadow_report(root: Path) -> dict[str, object]:
    archive = load_active_history_archive(root)
    cutoff = archive.snapshot.data_cutoff
    sessions = tuple(day for day in archive.calendar.open_dates if day <= cutoff)
    if not sessions:
        raise HistoryArchiveError("history_snapshot_calendar_empty")
    target_sessions = sessions[-_V2_SESSIONS:]
    target_start = target_sessions[0]
    universe = archive.universe.securities
    facts: dict[str, _CodeFacts] = {}
    for security in universe:
        expected = {
            day
            for day in target_sessions
            if day >= security.listed_on and (security.delisted_on is None or day < security.delisted_on)
        }
        facts[security.code] = _CodeFacts(expected, set(), set(), set())

    reader = SQLiteHistoryArchiveReader(archive.root)
    for code, trade_date, raw_present, qfq_present in reader.iter_shadow_facts(target_start, cutoff, archive.snapshot):
        fact = facts.get(code)
        if fact is None:
            continue
        key = trade_date
        if key in fact.observed:
            fact.duplicate_rows += 1
        fact.observed.add(key)
        if key not in fact.expected:
            fact.invalid_rows += 1
        if raw_present:
            fact.raw_dates.add(key)
        if qfq_present:
            fact.qfq_dates.add(key)

    expected_rows = sum(len(item.expected) for item in facts.values())
    observed_rows = sum(len(item.observed) for item in facts.values())
    raw_rows = sum(len(item.raw_dates) for item in facts.values())
    qfq_rows = sum(len(item.qfq_dates) for item in facts.values())
    raw_missing = sum(len(item.expected - item.raw_dates) for item in facts.values())
    qfq_missing = sum(len(item.expected - item.qfq_dates) for item in facts.values())
    pair_complete = sum(item.expected <= item.raw_dates & item.qfq_dates for item in facts.values())
    date_mismatches = sum(len(item.raw_dates ^ item.qfq_dates) for item in facts.values())
    duplicate_rows = sum(item.duplicate_rows for item in facts.values())
    invalid_rows = sum(item.invalid_rows for item in facts.values())

    recent_sessions = sessions[-_RECENT_REFRESH_SESSIONS:]
    recent_reusable = sum(
        all(day in item.raw_dates and day in item.qfq_dates for day in recent_sessions)
        for item in facts.values()
    )
    # One BaoStock query returns the complete requested date range. The five-day
    # refresh window therefore changes row volume, not the number of calls.
    baseline_calls = len(universe) * 2
    candidate_skips = recent_reusable
    return {
        "schema_version": "baostock-qfq-shadow",
        "status": "degraded",
        "production_eligible": False,
        "production_eligibility_reason": "no_verified_adjustment_factor_or_corporate_action_source",
        "active_snapshot": {
            "sequence": archive.snapshot.sequence,
            "content_hash": archive.snapshot.content_hash,
            "data_cutoff": cutoff.isoformat(),
            "label_cutoff": archive.snapshot.label_cutoff.isoformat(),
            "universe_count": len(universe),
        },
        "target_window": {
            "sessions": len(target_sessions),
            "v3_required_sessions": _V3_SESSIONS,
            "v2_required_sessions": _V2_SESSIONS,
            "start": target_start.isoformat(),
            "end": cutoff.isoformat(),
            "expected_rows": expected_rows,
            "observed_rows": observed_rows,
        },
        "raw_qfq_integrity": {
            "raw_rows": raw_rows,
            "qfq_rows": qfq_rows,
            "paired_security_count": pair_complete,
            "pair_complete_rate": round(pair_complete / len(universe), 6) if universe else 0.0,
            "raw_missing_rows": raw_missing,
            "qfq_missing_rows": qfq_missing,
            "raw_qfq_date_mismatches": date_mismatches,
            "duplicate_rows": duplicate_rows,
            "invalid_rows": invalid_rows,
        },
        "request_baseline": {
            "raw_qfq_queries_per_security": 2,
            "active_universe_raw_qfq_queries": baseline_calls,
            "recent_refresh_sessions": len(recent_sessions),
            "recent_refresh_raw_qfq_queries": baseline_calls,
            "recent_fully_paired_securities": recent_reusable,
            "theoretical_skippable_queries": candidate_skips,
            "theoretical_skip_rate": round(candidate_skips / baseline_calls, 6)
            if baseline_calls
            else 0.0,
            "interpretation": "candidate_reuse_only_requires_external_action_change_evidence",
        },
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        report = build_shadow_report(args.history_root.resolve())
    except (HistoryArchiveError, OSError, RuntimeError, TypeError, ValueError):
        emit_report(
            {
                "schema_version": "baostock-qfq-shadow",
                "status": "failed",
                "production_eligible": False,
            }
        )
        return 1
    emit_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
