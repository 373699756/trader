from __future__ import annotations

import argparse
import json
import sqlite3

from . import config
from .production_baseline import production_baseline_id
from .strategy_validation import StrategyValidationStore
from .validation_policy import (
    current_strategy_version,
    matches_current_validation_baseline,
    validation_baseline_config,
)


_READINESS_TABLES = (
    "strategy_signal_batches",
    "strategy_signals",
    "strategy_candidate_snapshots",
    "strategy_outcomes",
    "strategy_execution_records",
    "strategy_execution_skips",
    "daily_portfolio_baselines",
    "strategy_oos_reports",
    "strategy_fold_predictions",
    "strategy_deepseek_shadow_signals",
    "strategy_deepseek_shadow_outcomes",
)


def _table_exists(conn, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return bool(row)


def _count_rows(conn, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0] or 0)


def _count_distinct_days(conn, sql: str) -> int:
    try:
        return int(conn.execute(sql).fetchone()[0] or 0)
    except sqlite3.Error:
        return 0


def _current_version_chain(conn, strategy_name: str) -> dict:
    strategy_version = current_strategy_version(strategy_name)
    expected_production_baseline = production_baseline_id()
    expected_validation_baseline = str(
        validation_baseline_config(strategy_name).get("baseline_id") or ""
    )
    batches = conn.execute(
        """
        SELECT signal_date, saved_count, candidate_count, selected_count, generation_json
        FROM strategy_signal_batches
        WHERE strategy_name = ? AND strategy_version = ?
        ORDER BY signal_date
        """,
        (strategy_name, strategy_version),
    ).fetchall()
    signal_rows = conn.execute(
        """
        SELECT s.signal_date, s.id,
               COALESCE(o.validation_baseline_id, '') AS validation_baseline_id,
               CASE WHEN o.signal_id IS NULL THEN 0 ELSE 1 END AS has_outcome,
               COALESCE(e.label_status, o.label_status,
                        CASE WHEN o.signal_id IS NULL THEN 'pending' ELSE 'settled' END) AS label_status,
               COALESCE(e.promotion_eligible, 0) AS promotion_eligible,
               CASE WHEN e.signal_id IS NULL THEN 0 ELSE 1 END AS has_execution
        FROM strategy_signals s
        LEFT JOIN strategy_outcomes o ON o.signal_id = s.id
        LEFT JOIN strategy_execution_records e ON e.signal_id = s.id
        WHERE s.strategy_name = ? AND s.strategy_version = ?
        ORDER BY s.signal_date, s.rank
        """,
        (strategy_name, strategy_version),
    ).fetchall()
    candidate_count, candidate_days, selected_candidates = conn.execute(
        """
        SELECT COUNT(*), COUNT(DISTINCT signal_date), COALESCE(SUM(selected), 0)
        FROM strategy_candidate_snapshots
        WHERE strategy_name = ? AND strategy_version = ?
        """,
        (strategy_name, strategy_version),
    ).fetchone()

    production_match_count = 0
    nonempty_batch_rows = [row for row in batches if int(row[1] or 0) > 0]
    for _signal_date, _saved, _candidates, _selected, generation_json in nonempty_batch_rows:
        try:
            generation = json.loads(generation_json or "{}")
        except (TypeError, ValueError):
            generation = {}
        if (
            str(generation.get("baseline_id") or "") == expected_production_baseline
            and str(generation.get("baseline_status") or "") == "frozen"
            and not (generation.get("drift") or [])
        ):
            production_match_count += 1

    outcome_rows = [row for row in signal_rows if bool(row[3])]
    validation_match_count = sum(
        1
        for row in outcome_rows
        if matches_current_validation_baseline(
            row[2],
            strategy_name,
            expected_validation_baseline,
        )
    )
    settled_promotion_dates = {
        str(row[0])
        for row in signal_rows
        if str(row[4]) == "settled"
        and bool(row[5])
        and matches_current_validation_baseline(
            row[2],
            strategy_name,
            expected_validation_baseline,
        )
    }
    signal_days = len({str(row[0]) for row in signal_rows})
    nonempty_batches = len(nonempty_batch_rows)
    baseline_mismatch = (
        production_match_count < nonempty_batches
        or validation_match_count < len(outcome_rows)
    )
    if baseline_mismatch:
        status = "baseline_mismatch"
    elif signal_days < 5:
        status = "collecting"
    elif any(str(row[4]) in {"pending", "unknown"} for row in signal_rows):
        status = "labeling"
    else:
        status = "ready"
    return {
        "status": status,
        "strategy_name": strategy_name,
        "strategy_version": strategy_version,
        "batch_count": len(batches),
        "nonempty_batch_count": nonempty_batches,
        "empty_batch_count": len(batches) - nonempty_batches,
        "signal_count": len(signal_rows),
        "signal_days": signal_days,
        "candidate_count": int(candidate_count or 0),
        "candidate_days": int(candidate_days or 0),
        "selected_candidate_count": int(selected_candidates or 0),
        "execution_record_count": sum(1 for row in signal_rows if bool(row[6])),
        "outcome_count": len(outcome_rows),
        "pending_count": sum(1 for row in signal_rows if str(row[4]) == "pending"),
        "unknown_count": sum(1 for row in signal_rows if str(row[4]) == "unknown"),
        "promotion_eligible_count": sum(1 for row in signal_rows if bool(row[5])),
        "settled_promotion_days": len(settled_promotion_dates),
        "expected_production_baseline_id": expected_production_baseline,
        "production_baseline_match_count": production_match_count,
        "expected_validation_baseline_id": expected_validation_baseline,
        "validation_baseline_match_count": validation_match_count,
    }


def build_validation_readiness_report(db_path: str) -> dict:
    min_oos_days = int(getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60) or 60)
    formal_oos_days = max(120, min_oos_days)
    min_event_days = 60
    StrategyValidationStore(db_path)
    with sqlite3.connect(db_path) as conn:
        table_counts = {table: _count_rows(conn, table) for table in _READINESS_TABLES}
        current_version_chains = {
            strategy_name: _current_version_chain(conn, strategy_name)
            for strategy_name in ("short_term", "tomorrow_picks", "swing_picks")
        }
        real_oos_day_count = int(
            current_version_chains["tomorrow_picks"]["settled_promotion_days"]
        )
        portfolio_day_count = _count_distinct_days(
            conn,
            """
            SELECT COUNT(DISTINCT signal_date)
            FROM daily_portfolio_baselines
            WHERE COALESCE(status, '') IN ('settled', 'closed', 'ready', 'ok')
            """,
        )
        event_day_count = _count_distinct_days(
            conn,
            """
            SELECT COUNT(DISTINCT s.signal_date)
            FROM strategy_deepseek_shadow_outcomes o
            JOIN strategy_deepseek_shadow_signals s ON s.id = o.shadow_id
            """,
        )
    blockers = []
    for strategy_name, chain in current_version_chains.items():
        if int(chain["signal_days"]) < 5:
            blockers.append(
                {
                    "task": "P0-CURRENT-VERSION-SAMPLE-CHAIN",
                    "code": "current_version_signal_days_insufficient",
                    "strategy_name": strategy_name,
                    "strategy_version": chain["strategy_version"],
                    "observed_days": int(chain["signal_days"]),
                    "required_days": 5,
                    "missing_days": 5 - int(chain["signal_days"]),
                }
            )
    if real_oos_day_count < min_oos_days:
        blockers.append(
            {
                "task": "P3-REAL-OOS-SAMPLE-GATE",
                "code": "real_oos_days_insufficient",
                "observed_days": real_oos_day_count,
                "required_days": min_oos_days,
                "missing_days": min_oos_days - real_oos_day_count,
            }
        )
    if real_oos_day_count < min_oos_days:
        blockers.append(
            {
                "task": "P4-REBUILDABLE-RETURN-ARTIFACT",
                "code": "cannot_build_promotable_artifact_without_oos",
                "observed_days": real_oos_day_count,
                "required_days": min_oos_days,
                "missing_days": min_oos_days - real_oos_day_count,
            }
        )
    if portfolio_day_count < min_oos_days:
        blockers.append(
            {
                "task": "P5-PORTFOLIO-ABLATION-EVIDENCE",
                "code": "portfolio_oos_days_insufficient",
                "observed_days": portfolio_day_count,
                "required_days": min_oos_days,
                "missing_days": min_oos_days - portfolio_day_count,
            }
        )
    if event_day_count < min_event_days:
        blockers.append(
            {
                "task": "P6-DEEPSEEK-EVENT-COUNTERFACTUAL",
                "code": "event_days_insufficient",
                "observed_days": event_day_count,
                "required_days": min_event_days,
                "missing_days": min_event_days - event_day_count,
            }
        )
    if real_oos_day_count < formal_oos_days:
        blockers.append(
            {
                "task": "P7-GRAY-ROLLBACK",
                "code": "formal_oos_days_insufficient",
                "observed_days": real_oos_day_count,
                "required_days": formal_oos_days,
                "missing_days": formal_oos_days - real_oos_day_count,
            }
        )
    return {
        "ok": not blockers,
        "db_path": db_path,
        "table_counts": table_counts,
        "readiness": {
            "real_oos_day_count": real_oos_day_count,
            "portfolio_day_count": portfolio_day_count,
            "deepseek_event_day_count": event_day_count,
            "min_oos_days": min_oos_days,
            "formal_oos_days": formal_oos_days,
            "min_event_days": min_event_days,
        },
        "current_version_chains": current_version_chains,
        "blockers": blockers,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="审计 point-in-time 信号、执行标签与样本守恒")
    parser.add_argument("--db-path", default=config.VALIDATION_DB_PATH, help="验证数据库路径")
    parser.add_argument("--strategy", default="", help="仅审计指定策略")
    parser.add_argument("--sample-size", type=int, default=30, help="抽查已入选信号数量")
    parser.add_argument("--readiness", action="store_true", help="输出 OOS 样本、组合和 DeepSeek 事件门槛报告")
    args = parser.parse_args(argv)
    if args.readiness:
        report = build_validation_readiness_report(args.db_path)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.get("ok") else 1
    report = StrategyValidationStore(args.db_path).audit_point_in_time(
        strategy_name=args.strategy,
        sample_size=args.sample_size,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
