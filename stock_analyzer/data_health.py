from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Dict, Iterable, List

from . import config
from .experiment_registry import list_experiments, validate_experiment
from .normalization import coerce_number
from .pit_snapshot import PointInTimeSnapshotStore, REAL_FORWARD
from .production_baseline import production_baseline_id, production_baseline_status
from .runtime_json import atomic_write_json
from .strategy_validation import StrategyValidationStore
from .validation_audit_cli import build_validation_readiness_report
from .validation_policy import current_strategy_version
from .validation_statistics import unified_experiment_fdr


BASE_QUOTE_FIELDS = ("code", "name", "price", "pct_chg", "turnover")
EXECUTION_QUOTE_FIELDS = ("price", "turnover", "volume_ratio", "turnover_rate", "amplitude")
OPTIONAL_RESEARCH_FIELDS = (
    "main_net_flow_1d",
    "order_imbalance",
    "fundamental_quality_score",
)


def build_data_health_report(
    validation_db_path: str = "",
    market_data_db_path: str = "",
    generated_at: str = "",
) -> Dict[str, object]:
    validation_db_path = str(validation_db_path or config.VALIDATION_DB_PATH)
    market_data_db_path = str(market_data_db_path or config.MARKET_DATA_DB_PATH)
    generated_at = str(generated_at or datetime.now().isoformat(timespec="seconds"))
    StrategyValidationStore(validation_db_path)
    readiness = build_validation_readiness_report(validation_db_path)
    baseline = production_baseline_status()
    pit_store = PointInTimeSnapshotStore(validation_db_path)
    pit_summary = pit_store.summary()
    latest_market_snapshot = pit_summary.get("latest") or {}
    with sqlite3.connect(validation_db_path) as conn:
        conn.row_factory = sqlite3.Row
        samples = _sample_summary(conn)
        pit = _candidate_pit_summary(conn)
        costs = _cost_summary(conn)
        deepseek = _deepseek_summary(conn)
        experiments = _experiment_summary(conn)
    history = _market_history_summary(market_data_db_path)
    quote_freshness = _quote_freshness(latest_market_snapshot, generated_at)
    field_coverage = latest_market_snapshot.get("field_coverage") or {}
    coverage_gates = _coverage_gates(field_coverage)
    p0 = _p0_gate(baseline, readiness, pit, pit_summary)
    p1 = _p1_gate(samples, pit, pit_summary, history, coverage_gates)
    p2 = _p2_gate(readiness, experiments)
    blockers = [
        {"priority": priority, **item}
        for priority, gate in (("P0", p0), ("P1", p1), ("P2", p2))
        for item in gate.get("blockers") or []
    ]
    return {
        "schema_version": 1,
        "generated_at": generated_at,
        "status": "ready" if not blockers else "blocked",
        "ok": not blockers,
        "production_baseline": baseline,
        "readiness": readiness,
        "samples": samples,
        "point_in_time": {
            **pit,
            "market_snapshot": pit_summary,
            "quote_freshness": quote_freshness,
        },
        "factor_coverage": {
            "full_market": field_coverage,
            "candidate_and_top5": pit.get("feature_coverage") or {},
            "gates": coverage_gates,
        },
        "execution_costs": costs,
        "deepseek": deepseek,
        "market_history": history,
        "experiments": experiments,
        "gates": {"P0": p0, "P1": p1, "P2": p2},
        "blockers": blockers,
    }


def build_and_save_data_health_report(
    validation_db_path: str = "",
    market_data_db_path: str = "",
    output_path: str = "",
    archive_dir: str = "",
) -> Dict[str, object]:
    report = build_data_health_report(validation_db_path, market_data_db_path)
    target = str(
        output_path
        or getattr(config, "DATA_HEALTH_REPORT_PATH", ".runtime/data_health_report.json")
    )
    archive = str(
        archive_dir
        or getattr(config, "DATA_HEALTH_ARCHIVE_DIR", ".runtime/data_health")
    )
    atomic_write_json(target, report, ensure_ascii=False, indent=2)
    if archive:
        date_key = str(report.get("generated_at") or "")[:10] or datetime.now().date().isoformat()
        archive_path = os.path.join(archive, "{}.json".format(date_key))
        atomic_write_json(archive_path, report, ensure_ascii=False, indent=2)
    return {**report, "report_path": target}


def _sample_summary(conn) -> Dict[str, object]:
    result = {}
    for strategy in ("short_term", "tomorrow_picks", "swing_picks"):
        version = current_strategy_version(strategy)
        rows = conn.execute(
            """
            SELECT COALESCE(sample_type, 'unknown') AS sample_type,
                   COUNT(*) AS batch_count,
                   COUNT(DISTINCT CASE WHEN saved_count > 0 THEN signal_date END) AS day_count,
                   COALESCE(SUM(saved_count), 0) AS signal_count,
                   COALESCE(SUM(candidate_count), 0) AS candidate_count,
                   COALESCE(SUM(selected_count), 0) AS selected_count
            FROM strategy_signal_batches
            WHERE strategy_name = ? AND strategy_version = ?
            GROUP BY COALESCE(sample_type, 'unknown')
            """,
            (strategy, version),
        ).fetchall()
        by_type = {str(row[0]): dict(row) for row in rows}
        result[strategy] = {
            "strategy_version": version,
            "by_sample_type": by_type,
            "real_forward_day_count": int((by_type.get(REAL_FORWARD) or {}).get("day_count") or 0),
            "replay_day_count": sum(
                int(item.get("day_count") or 0)
                for key, item in by_type.items()
                if key in {"daily_proxy_replay", "intraday_pit_replay"}
            ),
            "legacy_day_count": int((by_type.get("legacy_baseline") or {}).get("day_count") or 0),
            "unknown_day_count": int((by_type.get("unknown") or {}).get("day_count") or 0),
        }
    return result


def _candidate_pit_summary(conn) -> Dict[str, object]:
    result = {}
    all_missing_masks = []
    violation_counter = Counter()
    for strategy in ("short_term", "tomorrow_picks", "swing_picks"):
        version = current_strategy_version(strategy)
        aggregate = conn.execute(
            """
            SELECT COUNT(*) AS candidate_count,
                   COALESCE(SUM(selected), 0) AS selected_count,
                   COALESCE(SUM(CASE WHEN point_in_time_valid = 1 THEN 1 ELSE 0 END), 0) AS valid_count,
                   COALESCE(SUM(CASE WHEN selected = 1 AND point_in_time_valid = 1 THEN 1 ELSE 0 END), 0) AS selected_valid_count,
                   COUNT(DISTINCT signal_date) AS day_count,
                   MAX(signal_date) AS latest_signal_date
            FROM strategy_candidate_snapshots
            WHERE strategy_name = ? AND strategy_version = ?
            """,
            (strategy, version),
        ).fetchone()
        latest_date = str(aggregate[5] or "") if aggregate else ""
        mask_rows = []
        if latest_date:
            mask_rows = conn.execute(
                """
                SELECT eligible, selected, missing_mask_json, point_in_time_violations_json
                FROM strategy_candidate_snapshots
                WHERE strategy_name = ? AND strategy_version = ? AND signal_date = ?
                """,
                (strategy, version, latest_date),
            ).fetchall()
        for row in mask_rows:
            mask = _load_json(row[2], {})
            all_missing_masks.append(
                {"eligible": bool(row[0]), "selected": bool(row[1]), "mask": mask}
            )
            for violation in _load_json(row[3], []):
                violation_counter[str(violation).split(":", 1)[0]] += 1
        candidate_count = int(aggregate[0] or 0) if aggregate else 0
        selected_count = int(aggregate[1] or 0) if aggregate else 0
        valid_count = int(aggregate[2] or 0) if aggregate else 0
        selected_valid = int(aggregate[3] or 0) if aggregate else 0
        result[strategy] = {
            "strategy_version": version,
            "candidate_count": candidate_count,
            "selected_count": selected_count,
            "valid_count": valid_count,
            "point_in_time_valid_pct": _pct(valid_count, candidate_count),
            "selected_point_in_time_valid_pct": _pct(selected_valid, selected_count),
            "day_count": int(aggregate[4] or 0) if aggregate else 0,
            "latest_signal_date": latest_date,
        }
    return {
        "strategies": result,
        "violation_types": dict(violation_counter),
        "feature_coverage": _missing_mask_coverage(all_missing_masks),
    }


def _missing_mask_coverage(rows: List[Dict[str, object]]) -> Dict[str, object]:
    fields = sorted({str(field) for row in rows for field in (row.get("mask") or {})})
    result = {}
    for scope, predicate in (
        ("full_market", lambda row: True),
        ("candidate_pool", lambda row: bool(row.get("eligible"))),
        ("top5", lambda row: bool(row.get("selected"))),
    ):
        scoped = [row for row in rows if predicate(row)]
        result[scope] = {
            field: {
                "coverage_pct": _pct(
                    sum(1 for row in scoped if not bool((row.get("mask") or {}).get(field, True))),
                    len(scoped),
                ),
                "sample_count": len(scoped),
            }
            for field in fields
        }
    return result


def _cost_summary(conn) -> Dict[str, object]:
    rows = conn.execute(
        """
        SELECT e.fee_pct, e.slippage_pct, e.impact_pct,
               e.fee_pct + e.slippage_pct + e.impact_pct AS total_pct
        FROM strategy_execution_records e
        JOIN strategy_signals s ON s.id = e.signal_id
        WHERE e.label_status IN ('settled', 'unfilled')
        """
    ).fetchall()
    return {
        "sample_count": len(rows),
        "fee_pct": _distribution(row[0] for row in rows),
        "slippage_pct": _distribution(row[1] for row in rows),
        "impact_pct": _distribution(row[2] for row in rows),
        "total_pct": _distribution(row[3] for row in rows),
    }


def _deepseek_summary(conn) -> Dict[str, object]:
    row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(candidate_count), 0), COALESCE(SUM(valid_count), 0),
               COALESCE(SUM(abstain_count), 0), COALESCE(SUM(rejected_count), 0),
               COALESCE(SUM(api_called), 0),
               COALESCE(SUM(CASE WHEN status IN ('failed', 'error') OR error_type <> '' THEN 1 ELSE 0 END), 0)
        FROM deepseek_analysis_batches
        """
    ).fetchone()
    return {
        "batch_count": int(row[0] or 0),
        "candidate_count": int(row[1] or 0),
        "valid_count": int(row[2] or 0),
        "abstain_count": int(row[3] or 0),
        "rejected_count": int(row[4] or 0),
        "api_call_count": int(row[5] or 0),
        "api_failure_count": int(row[6] or 0),
    }


def _experiment_summary(conn) -> Dict[str, object]:
    records = []
    valid_records = []
    invalid = []
    try:
        records = list_experiments()
        for record in records:
            try:
                valid_records.append(validate_experiment(record))
            except Exception as exc:
                invalid.append({"experiment_id": record.get("experiment_id"), "error": str(exc)})
    except Exception as exc:
        invalid.append({"experiment_id": "", "error": str(exc)})
    table_counts = {
        table: int(conn.execute("SELECT COUNT(*) FROM {}".format(table)).fetchone()[0] or 0)
        for table in ("strategy_fold_predictions", "strategy_oos_reports", "daily_portfolio_baselines")
    }
    unified_fdr = unified_experiment_fdr(
        valid_records,
        q=coerce_number(getattr(config, "EXPERIMENT_FDR_Q", 0.1), 0.1),
    )
    paired_increment_day_count = 0
    dsr_ready_report_count = 0
    dsr_passed_report_count = 0
    statistical_gate_passed_report_count = 0
    for row in conn.execute(
        "SELECT report_json FROM strategy_oos_reports ORDER BY generated_at DESC LIMIT 120"
    ).fetchall():
        report = _load_json(row[0], {})
        challenger = report.get("challenger_statistics") if isinstance(report, dict) else {}
        challenger = challenger if isinstance(challenger, dict) else {}
        paired = challenger.get("paired_daily_increments") or []
        paired_increment_day_count = max(paired_increment_day_count, len(paired))
        statistics_result = challenger.get("paired_statistics") or {}
        dsr = statistics_result.get("dsr") if isinstance(statistics_result, dict) else {}
        if isinstance(dsr, dict) and dsr.get("status") == "ready":
            dsr_ready_report_count += 1
        if isinstance(dsr, dict) and dsr.get("passed"):
            dsr_passed_report_count += 1
        promotion_gate = challenger.get("promotion_gate") or {}
        if isinstance(promotion_gate, dict) and promotion_gate.get("status") == "passed":
            statistical_gate_passed_report_count += 1
    return {
        "registered_count": len(records),
        "valid_registered_count": len(valid_records),
        "baseline_mismatch_count": sum(
            1
            for row in valid_records
            if str(row.get("production_baseline_id") or "") != str(production_baseline_id() or "")
        ),
        "challenger_registered_count": sum(
            1 for row in valid_records if str(row.get("experiment_family") or "") != "baseline_freeze"
        ),
        "registered_ids": [str(row.get("experiment_id") or "") for row in records],
        "invalid_records": invalid,
        "unified_fdr": unified_fdr,
        "paired_increment_day_count": paired_increment_day_count,
        "dsr_ready_report_count": dsr_ready_report_count,
        "dsr_passed_report_count": dsr_passed_report_count,
        "statistical_gate_passed_report_count": statistical_gate_passed_report_count,
        **table_counts,
    }


def _market_history_summary(db_path: str) -> Dict[str, object]:
    if not os.path.isfile(db_path):
        return {"db_path": db_path, "exists": False, "bar_count": 0, "stock_count": 0}
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), COUNT(DISTINCT code), COUNT(DISTINCT trade_date),
                       MIN(trade_date), MAX(trade_date)
                FROM daily_bars
                """
            ).fetchone()
        return {
            "db_path": db_path,
            "exists": True,
            "bar_count": int(row[0] or 0),
            "stock_count": int(row[1] or 0),
            "trade_day_count": int(row[2] or 0),
            "date_start": str(row[3] or ""),
            "date_end": str(row[4] or ""),
        }
    except sqlite3.Error as exc:
        return {"db_path": db_path, "exists": True, "error": str(exc), "bar_count": 0, "stock_count": 0}


def _quote_freshness(snapshot: Dict[str, object], generated_at: str) -> Dict[str, object]:
    captured_at = str(snapshot.get("captured_at") or "")
    observed_at = str(snapshot.get("data_source_timestamp") or "")
    age_seconds = None
    try:
        age_seconds = max(
            0.0,
            (datetime.fromisoformat(generated_at) - datetime.fromisoformat(observed_at)).total_seconds(),
        )
    except (TypeError, ValueError):
        pass
    captured_time = captured_at[11:19] if len(captured_at) >= 19 else ""
    return {
        "snapshot_id": str(snapshot.get("snapshot_id") or ""),
        "captured_at": captured_at,
        "observed_at": observed_at,
        "age_seconds": age_seconds,
        "in_1430_1450_window": bool("14:30:00" <= captured_time < "14:50:00"),
    }


def _coverage_gates(coverage: Dict[str, object]) -> Dict[str, object]:
    def value(field):
        return coerce_number((coverage.get(field) or {}).get("coverage_pct"), 0.0)

    base = {field: value(field) for field in BASE_QUOTE_FIELDS}
    execution = {field: value(field) for field in EXECUTION_QUOTE_FIELDS}
    optional = {field: value(field) for field in OPTIONAL_RESEARCH_FIELDS}
    return {
        "base_quote": {"required_pct": 98.0, "coverage": base, "passed": bool(base) and min(base.values()) >= 98.0},
        "hard_filter_execution": {
            "required_pct": 99.0,
            "coverage": execution,
            "passed": bool(execution) and min(execution.values()) >= 99.0,
        },
        "optional_research": {
            "required_pct": 95.0,
            "coverage": optional,
            "eligible_fields": [field for field, coverage_pct in optional.items() if coverage_pct >= 95.0],
        },
    }


def _p0_gate(baseline, readiness, pit, pit_summary) -> Dict[str, object]:
    blockers = []
    if baseline.get("status") != "frozen" or baseline.get("drift"):
        blockers.append({"code": "production_baseline_not_frozen"})
    for strategy, chain in (readiness.get("current_version_chains") or {}).items():
        if int(chain.get("signal_days") or 0) < 5:
            blockers.append(
                {
                    "code": "current_version_signal_days_insufficient",
                    "strategy": strategy,
                    "observed": int(chain.get("signal_days") or 0),
                    "required": 5,
                }
            )
        if int(chain.get("unknown_count") or 0) > 0:
            blockers.append({"code": "unknown_outcomes_present", "strategy": strategy})
    if int(pit_summary.get("snapshot_count") or 0) < 1:
        blockers.append({"code": "raw_market_snapshot_missing"})
    return {"status": "complete" if not blockers else "collecting", "complete": not blockers, "blockers": blockers}


def _p1_gate(samples, pit, pit_summary, history, coverage_gates) -> Dict[str, object]:
    blockers = []
    real_days = int(((samples.get("tomorrow_picks") or {}).get("real_forward_day_count") or 0))
    if real_days < 60:
        blockers.append({"code": "real_forward_days_insufficient", "observed": real_days, "required": 60})
    top5_rates = [
        coerce_number(item.get("selected_point_in_time_valid_pct"), 0.0)
        for item in (pit.get("strategies") or {}).values()
        if int(item.get("selected_count") or 0) > 0
    ]
    if not top5_rates or min(top5_rates) < 100.0:
        blockers.append({"code": "top5_point_in_time_validity_below_100"})
    for key in ("base_quote", "hard_filter_execution"):
        if not bool((coverage_gates.get(key) or {}).get("passed")):
            blockers.append({"code": "{}_coverage_below_threshold".format(key)})
    latest_quote_count = int(((pit_summary.get("latest") or {}).get("quote_count") or 0))
    latest_snapshot = pit_summary.get("latest") or {}
    latest_sample_type = str(latest_snapshot.get("sample_type") or "unknown")
    latest_observed_at = str(latest_snapshot.get("data_source_timestamp") or "")
    latest_captured_at = str(latest_snapshot.get("captured_at") or "")
    latest_clock = latest_captured_at[11:16] if len(latest_captured_at) >= 16 else ""
    if latest_sample_type != REAL_FORWARD:
        blockers.append(
            {
                "code": "latest_market_snapshot_not_real_forward",
                "sample_type": latest_sample_type,
            }
        )
    if not latest_observed_at or not ("14:30" <= latest_clock < "14:50"):
        blockers.append(
            {
                "code": "latest_market_snapshot_time_invalid",
                "captured_at": latest_captured_at,
                "observed_at": latest_observed_at,
            }
        )
    stock_count = int(history.get("stock_count") or 0)
    universe_coverage = coerce_number(_pct(stock_count, latest_quote_count), 0.0)
    if latest_quote_count <= 0 or universe_coverage < 98.0:
        blockers.append(
            {
                "code": "historical_universe_coverage_below_98",
                "coverage_pct": universe_coverage,
                "stock_count": stock_count,
                "universe_count": latest_quote_count,
            }
        )
    if int(history.get("trade_day_count") or 0) < 480:
        blockers.append({"code": "historical_trade_days_below_two_year_target"})
    return {"status": "complete" if not blockers else "collecting", "complete": not blockers, "blockers": blockers}


def _p2_gate(readiness, experiments) -> Dict[str, object]:
    blockers = []
    if int(experiments.get("challenger_registered_count") or 0) <= 0:
        blockers.append({"code": "challenger_experiment_not_registered"})
    if experiments.get("invalid_records"):
        blockers.append({"code": "experiment_registry_invalid"})
    if int(experiments.get("baseline_mismatch_count") or 0) > 0:
        blockers.append({"code": "experiment_baseline_mismatch"})
    if int(experiments.get("strategy_fold_predictions") or 0) <= 0:
        blockers.append({"code": "oos_fold_predictions_missing"})
    if int(experiments.get("strategy_oos_reports") or 0) <= 0:
        blockers.append({"code": "oos_report_missing"})
    min_oos_days = int((readiness.get("readiness") or {}).get("min_oos_days") or 60)
    if int(experiments.get("paired_increment_day_count") or 0) < min_oos_days:
        blockers.append(
            {
                "code": "paired_daily_increment_days_insufficient",
                "observed": int(experiments.get("paired_increment_day_count") or 0),
                "required": min_oos_days,
            }
        )
    unified_fdr = experiments.get("unified_fdr") or {}
    if int(unified_fdr.get("reported_trial_count") or 0) <= 0:
        blockers.append({"code": "unified_fdr_results_missing"})
    elif int(unified_fdr.get("rejected_count") or 0) <= 0:
        blockers.append({"code": "unified_fdr_not_passed"})
    if int(experiments.get("dsr_passed_report_count") or 0) <= 0:
        blockers.append({"code": "deflated_sharpe_not_passed"})
    if int(experiments.get("statistical_gate_passed_report_count") or 0) <= 0:
        blockers.append({"code": "unified_challenger_gate_not_passed"})
    if int((readiness.get("readiness") or {}).get("real_oos_day_count") or 0) < int(
        min_oos_days
    ):
        blockers.append({"code": "real_oos_days_insufficient"})
    return {"status": "complete" if not blockers else "blocked", "complete": not blockers, "blockers": blockers}


def _distribution(values: Iterable[object]) -> Dict[str, object]:
    clean = sorted(coerce_number(value) for value in values if value is not None)
    if not clean:
        return {"count": 0, "mean": None, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(clean),
        "mean": round(sum(clean) / len(clean), 6),
        "min": round(clean[0], 6),
        "p50": _quantile(clean, 0.5),
        "p95": _quantile(clean, 0.95),
        "max": round(clean[-1], 6),
    }


def _quantile(values: List[float], probability: float):
    if not values:
        return None
    position = (len(values) - 1) * max(0.0, min(1.0, probability))
    low = int(position)
    high = min(len(values) - 1, low + 1)
    weight = position - low
    return round(values[low] * (1.0 - weight) + values[high] * weight, 6)


def _pct(numerator: object, denominator: object):
    denominator_value = int(coerce_number(denominator, 0.0))
    if denominator_value <= 0:
        return None
    return round(coerce_number(numerator, 0.0) * 100.0 / denominator_value, 4)


def _load_json(value: object, fallback):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError):
        return fallback
