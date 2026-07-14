from __future__ import annotations

from datetime import datetime
from collections import Counter
from typing import Callable, Dict

from . import config
from .experiment_registry import list_experiments as _list_experiments
from .normalization import coerce_number


def _experiment_audit_summary(strategy: str) -> Dict[str, object]:
    strategy = str(strategy or "").strip()
    try:
        experiments = _list_experiments()
    except Exception as exc:
        return {
            "status": "unavailable",
            "error": str(exc),
            "total_registered_experiments": 0,
            "strategy_registered_experiments": 0,
            "registered_trials_for_strategy": 0,
        }
    all_records = list(experiments or [])
    strategy_records = [
        row
        for row in all_records
        if str(row.get("strategy") or "").strip() == strategy
    ]
    latest_registered_at = ""
    latest_registered_at_ts = None
    fallback_latest = ""
    for record in all_records:
        raw_at = str(record.get("registered_at") or "").strip()
        if not raw_at:
            continue
        try:
            parsed = datetime.fromisoformat(raw_at.replace("Z", "+00:00"))
        except Exception:
            if raw_at > fallback_latest:
                fallback_latest = raw_at
                latest_registered_at = raw_at
            continue
        if latest_registered_at_ts is None or parsed > latest_registered_at_ts:
            latest_registered_at_ts = parsed
            latest_registered_at = raw_at
    decision_counter = Counter(
        str(row.get("decision") or "unknown") for row in strategy_records
    )
    result_counter = Counter(str(row.get("result") or "unknown") for row in strategy_records)
    return {
        "status": "ok",
        "total_registered_experiments": len(all_records),
        "strategy_registered_experiments": len(strategy_records),
        "registered_trial_ids_for_strategy": [
            str(record.get("experiment_id") or "")
            for record in strategy_records
            if str(record.get("experiment_id") or "").strip()
        ],
        "registered_trials_for_strategy": len(strategy_records),
        "decision_distribution": dict(decision_counter),
        "result_distribution": dict(result_counter),
        "latest_registered_at": latest_registered_at,
    }


def build_strategy_oos_report(
    strategy: str,
    days: int,
    metrics: Dict[str, object],
    baseline_status: Dict[str, object],
    gate_decision: Dict[str, object],
    generated_at: str = "",
    portfolio_baseline: Dict[str, object] = None,
) -> Dict[str, object]:
    strategy = str(strategy or "")
    days = int(days or 0)
    metrics = metrics or {}
    baseline_status = baseline_status or {}
    gate_decision = gate_decision or {}
    portfolio_baseline = portfolio_baseline or {}
    if baseline_status.get("needs_backfill"):
        status = "needs_backfill"
    elif not metrics.get("sample_count") and not metrics.get("outcome_sample_count"):
        status = "empty"
    elif not baseline_status.get("oos_ready"):
        status = "insufficient_oos_days"
    elif gate_decision.get("blocked"):
        status = "gate_blocked"
    else:
        status = "oos_passed"
    min_oos_days = int(
        baseline_status.get("min_oos_days")
        or getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60)
        or 0
    )
    ready_days = int(
        baseline_status.get("current_primary_ready_day_count")
        or metrics.get("real_day_count")
        or 0
    )
    missing_oos_days = max(0, min_oos_days - ready_days)
    blockers = []
    if status in ("empty", "insufficient_oos_days"):
        blockers.append(
            {
                "code": "real_oos_days_insufficient",
                "message": "真实 OOS 交易日不足，不能生成晋级结论或收益模型 artifact。",
                "observed_days": ready_days,
                "required_days": min_oos_days,
                "missing_days": missing_oos_days,
            }
        )
    elif status == "needs_backfill":
        blockers.append(
            {
                "code": "current_baseline_backfill_required",
                "message": "存在待回填或旧口径结果，不能混算当前 baseline。",
                "pending_count": int(baseline_status.get("pending_current_baseline_count") or 0),
                "mismatched_count": int(baseline_status.get("mismatched_baseline_outcome_count") or 0),
            }
        )
    elif status == "gate_blocked":
        blockers.append(
            {
                "code": "validation_gate_blocked",
                "message": str(gate_decision.get("reason") or "验证门控未通过。"),
            }
        )
    summary = {
        "sample_count": int(metrics.get("sample_count") or 0),
        "outcome_sample_count": int(metrics.get("outcome_sample_count") or 0),
        "real_sample_count": int(metrics.get("real_sample_count") or 0),
        "real_day_count": int(metrics.get("real_day_count") or 0),
        "avg_primary_return_net": metrics.get("avg_primary_return_net", 0.0),
        "win_rate_primary_net": metrics.get("win_rate_primary_net", 0.0),
        "real_avg_primary_return_net": metrics.get("real_avg_primary_return_net", 0.0),
        "real_win_rate_primary_net": metrics.get("real_win_rate_primary_net", 0.0),
        "real_avg_primary_return_net_ci95_low": metrics.get("real_avg_primary_return_net_ci95_low"),
        "real_avg_primary_return_net_ci95_high": metrics.get("real_avg_primary_return_net_ci95_high"),
        "real_win_rate_primary_net_ci95_low": metrics.get("real_win_rate_primary_net_ci95_low"),
        "real_portfolio_max_drawdown_pct": metrics.get("real_portfolio_max_drawdown_pct", 0.0),
        "avg_trade_cost_pct": metrics.get("avg_trade_cost_pct", 0.0),
        "survivorship_corrected_count": int(metrics.get("survivorship_corrected_count") or 0),
        "top_k_sensitivity": metrics.get("top_k_sensitivity") or {},
        "ready_oos_day_count": ready_days,
        "missing_oos_day_count": missing_oos_days,
    }
    frozen_portfolio = (portfolio_baseline.get("groups") or {}).get("frozen_rule_top_k") or {}
    portfolio_day_count = int(portfolio_baseline.get("day_count") or 0)
    portfolio_total_return = coerce_number(frozen_portfolio.get("total_return_pct"), 0.0)
    portfolio_ci_low_raw = frozen_portfolio.get("avg_daily_net_return_ci95_low")
    portfolio_ci_low = (
        coerce_number(portfolio_ci_low_raw)
        if portfolio_ci_low_raw is not None
        else None
    )
    require_positive_ci = bool(
        getattr(config, "STRATEGY_VALIDATION_REQUIRE_POSITIVE_CI", True)
    )
    portfolio_pending_days = int(portfolio_baseline.get("pending_day_count") or 0)
    portfolio_unknown_days = int(portfolio_baseline.get("unknown_day_count") or 0)
    if status == "oos_passed" and (
        portfolio_day_count < min_oos_days
        or portfolio_pending_days > 0
        or portfolio_unknown_days > 0
        or portfolio_total_return <= 0
        or (require_positive_ci and (portfolio_ci_low is None or portfolio_ci_low <= 0))
    ):
        status = "portfolio_blocked"
        blockers.append(
            {
                "code": "portfolio_baseline_blocked",
                "message": "冻结规则日级组合天数、持仓状态或累计净收益未通过，不能晋级。",
                "portfolio_day_count": portfolio_day_count,
                "required_days": min_oos_days,
                "portfolio_pending_day_count": portfolio_pending_days,
                "portfolio_unknown_day_count": portfolio_unknown_days,
                "portfolio_total_return_pct": portfolio_total_return,
                "portfolio_avg_daily_net_return_ci95_low": portfolio_ci_low_raw,
            }
        )
    summary.update(
        {
            "portfolio_day_count": portfolio_day_count,
            "portfolio_total_return_pct": portfolio_total_return,
            "portfolio_max_drawdown_pct": frozen_portfolio.get("max_drawdown_pct", 0.0),
            "portfolio_avg_daily_net_return_ci95_low": frozen_portfolio.get(
                "avg_daily_net_return_ci95_low"
            ),
            "portfolio_avg_daily_net_return_ci95_high": frozen_portfolio.get(
                "avg_daily_net_return_ci95_high"
            ),
            "portfolio_sortino": frozen_portfolio.get("sortino"),
            "portfolio_random_percentile": portfolio_baseline.get("rule_vs_random_percentile"),
        }
    )
    experiment_audit = gate_decision.get("experiment_audit")
    if not isinstance(experiment_audit, dict):
        experiment_audit = _experiment_audit_summary(strategy)
    summary["experiment_audit"] = experiment_audit
    requirements = {
        "min_oos_days": min_oos_days,
        "min_real_days": int(
            getattr(
                config,
                "STRATEGY_DECAY_MIN_REAL_DAYS",
                getattr(config, "STRATEGY_DECAY_MIN_REAL_SAMPLES", 20),
            )
        ),
        "min_win_rate": coerce_number(getattr(config, "STRATEGY_VALIDATION_MIN_WIN_RATE", 50.0), 50.0),
        "max_drawdown_floor_pct": coerce_number(
            getattr(config, "STRATEGY_VALIDATION_MAX_AVG_DRAWDOWN_PCT", -8.0),
            -8.0,
        ),
        "require_positive_ci": bool(getattr(config, "STRATEGY_VALIDATION_REQUIRE_POSITIVE_CI", True)),
    }
    return {
        "strategy": strategy,
        "days": days,
        "generated_at": generated_at or datetime.now().isoformat(timespec="seconds"),
        "oos_status": status,
        "can_promote": status == "oos_passed",
        "production_eligible": False,
        "promotion_stage": "shadow_eligible" if status == "oos_passed" else "blocked",
        "readiness": {
            "ready_oos_day_count": ready_days,
            "min_oos_days": min_oos_days,
            "missing_oos_day_count": missing_oos_days,
            "blocked_by_real_oos_days": status in ("empty", "insufficient_oos_days"),
        },
        "blockers": blockers,
        "validation_baseline": metrics.get("validation_baseline") or baseline_status.get("validation_baseline"),
        "validation_baseline_id": metrics.get("validation_baseline_id")
        or baseline_status.get("validation_baseline_id"),
        "baseline_status": baseline_status,
        "validation_gate": gate_decision,
        "experiment_audit": experiment_audit,
        "portfolio_baseline": portfolio_baseline,
        "summary": summary,
        "requirements": requirements,
    }


def generate_strategy_oos_report(
    validation_store,
    strategy: str,
    days: int,
    gate_decision_fn: Callable[[Dict[str, object], str], Dict[str, object]],
) -> Dict[str, object]:
    metrics = validation_store.metrics(strategy, days=days)
    baseline_status = validation_store.validation_baseline_status(strategy, days=days)
    gate_decision = gate_decision_fn(metrics, strategy)
    try:
        from .portfolio_baseline import DailyPortfolioBaselineService

        portfolio_baseline = DailyPortfolioBaselineService(validation_store).report(strategy, days=days)
    except Exception:
        portfolio_baseline = {}
    return build_strategy_oos_report(
        strategy,
        days,
        metrics,
        baseline_status,
        gate_decision,
        portfolio_baseline=portfolio_baseline,
    )
