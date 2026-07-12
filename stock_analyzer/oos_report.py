from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict

from . import config
from .normalization import coerce_number


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
    if status == "oos_passed" and portfolio_day_count > 0 and portfolio_total_return <= 0:
        status = "portfolio_blocked"
        blockers.append(
            {
                "code": "portfolio_baseline_blocked",
                "message": "冻结规则日级组合收益未通过，不能晋级。",
                "portfolio_day_count": portfolio_day_count,
                "portfolio_total_return_pct": portfolio_total_return,
            }
        )
    summary.update(
        {
            "portfolio_day_count": portfolio_day_count,
            "portfolio_total_return_pct": portfolio_total_return,
            "portfolio_max_drawdown_pct": frozen_portfolio.get("max_drawdown_pct", 0.0),
            "portfolio_sortino": frozen_portfolio.get("sortino"),
            "portfolio_random_percentile": portfolio_baseline.get("rule_vs_random_percentile"),
        }
    )
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
