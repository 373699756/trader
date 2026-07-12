from __future__ import annotations

import argparse
import json
import sqlite3

from . import config
from .strategy_validation import StrategyValidationStore


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


def build_validation_readiness_report(db_path: str) -> dict:
    min_oos_days = int(getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60) or 60)
    formal_oos_days = max(120, min_oos_days)
    min_event_days = 60
    StrategyValidationStore(db_path)
    with sqlite3.connect(db_path) as conn:
        table_counts = {table: _count_rows(conn, table) for table in _READINESS_TABLES}
        real_oos_day_count = _count_distinct_days(
            conn,
            """
            SELECT COUNT(DISTINCT s.signal_date)
            FROM strategy_outcomes o
            JOIN strategy_signals s ON s.id = o.signal_id
            WHERE COALESCE(o.label_status, 'settled') = 'settled'
            """,
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
