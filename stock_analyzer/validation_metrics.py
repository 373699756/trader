from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Dict, List

from . import config
from .normalization import coerce_number, normalize_code
from .validation_policy import (
    current_replay_strategy_version,
    current_strategy_version,
    execution_cost_pct as _execution_cost_pct,
    exit_holding_days as _exit_holding_days,
    is_primary_validation_signal as _is_primary_validation_signal,
    is_replay_version as _is_replay_version,
    legacy_validation_baseline_id,
    matches_current_validation_baseline as _matches_current_validation_baseline,
    outcome_ready as _outcome_ready,
    primary_return_config as _primary_return_config,
    stored_or_current_trade_cost_pct as _stored_or_current_trade_cost_pct,
    stored_validation_baseline_id as _stored_validation_baseline_id,
    validation_baseline_config,
)
from .validation_statistics import (
    average as _avg,
    daily_metrics as _daily_metrics,
    deepseek_action as _deepseek_action,
    deepseek_avoid_or_veto as _deepseek_avoid_or_veto,
    deepseek_blend_alpha as _deepseek_blend_alpha,
    deepseek_budget_recommendation as _deepseek_budget_recommendation,
    deepseek_counterfactual_topn as _deepseek_counterfactual_topn,
    deepseek_covered as _deepseek_covered,
    deepseek_group_delta as _deepseek_group_delta,
    deepseek_local_rank as _deepseek_local_rank,
    deepseek_shadow_rank as _deepseek_shadow_rank,
    deepseek_token_cost_summary as _deepseek_token_cost_summary,
    deepseek_token_value_metrics as _deepseek_token_value_metrics,
    has_deepseek_review as _has_deepseek_review,
    mean_confidence_interval as _mean_confidence_interval,
    next_day_compare as _next_day_compare,
    portfolio_max_drawdown as _portfolio_max_drawdown,
    rate as _rate,
    return_summary as _return_summary,
    top_k_sensitivity as _top_k_sensitivity,
    wilson_lower_bound as _wilson_lower_bound,
    write_deepseek_attribution_snapshot as _write_deepseek_attribution_snapshot,
)


class ValidationMetricsService:
    """Computes validation metrics and DeepSeek attribution."""

    def __init__(self, store) -> None:
        self.store = store
        self.repository = store.repository

    def metrics(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        current_version = current_strategy_version(strategy_name)
        signal_status = self.repository.signal_status_counts(
            strategy_name=strategy_name,
            days=days,
            strategy_version=current_version,
        )
        validation_baseline = validation_baseline_config(strategy_name)
        current_baseline_id = str(validation_baseline.get("baseline_id") or "")
        rows = self.repository.fetch_validation_metric_rows(
            strategy_name=strategy_name,
            current_version=current_version,
            replay_version=current_replay_strategy_version(strategy_name),
        )
        execution_skipped_count = self.repository.execution_skip_count(
            strategy_name=strategy_name,
            days=days,
            strategy_version=current_version,
        )
        if not rows:
            if strategy_name:
                primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
            else:
                primary_column, primary_days, primary_label = "strategy_primary_return", 0, "混合主周期"
            return {
                "strategy_name": strategy_name,
                "strategy_version": current_version,
                "sample_count": 0,
                "outcome_sample_count": 0,
                "total_sample_count": 0,
                "total_outcome_sample_count": 0,
                "backup_sample_count": 0,
                "backup_outcome_sample_count": 0,
                "real_sample_count": 0,
                "replay_sample_count": 0,
                "real_outcome_sample_count": 0,
                "replay_outcome_sample_count": 0,
                "real_total_sample_count": 0,
                "replay_total_sample_count": 0,
                "real_backup_sample_count": 0,
                "replay_backup_sample_count": 0,
                "real_day_count": 0,
                "replay_day_count": 0,
                "day_count": 0,
                "primary_return_field": primary_column,
                "primary_holding_days": primary_days,
                "primary_horizon_label": primary_label,
                "validation_baseline": validation_baseline,
                "validation_baseline_id": validation_baseline["baseline_id"],
                "current_baseline_outcome_count": 0,
                "raw_outcome_sample_count": 0,
                "legacy_baseline_outcome_count": 0,
                "excluded_baseline_mismatch_count": 0,
                "excluded_promotion_ineligible_count": 0,
                "avg_next_day_return_net": 0.0,
                "win_rate_next_day_net": 0.0,
                "avg_1_5d_exit_return_net": 0.0,
                "win_rate_1_5d_exit_net": 0.0,
                "positive_2_5d_after_weak_next_day_rate": 0.0,
                "auxiliary_exit_sample_count": 0,
                "weak_next_day_1_5d_sample_count": 0,
                "avg_max_drawdown_primary": 0.0,
                "real_avg_max_drawdown_primary": 0.0,
                "real_avg_primary_return_net_ci95_low": None,
                "real_avg_primary_return_net_ci95_high": None,
                "real_win_rate_primary_net_ci95_low": None,
                "real_portfolio_max_drawdown_pct": 0.0,
                "top_k_sensitivity": _top_k_sensitivity(
                    [],
                    getattr(config, "RESEARCH_TOP_K_SENSITIVITY", (3, 5, 10)),
                ),
                "survivorship_corrected_count": 0,
                "survivor_sample_count": 0,
                "avg_primary_return_net_survivors": 0.0,
                "win_rate_primary_net_survivors": 0.0,
                "avg_primary_return_net_all": 0.0,
                "win_rate_primary_net_all": 0.0,
                "win_rate_all": 0.0,
                "win_rate_survivors": 0.0,
                "execution_skipped_count": execution_skipped_count,
                **signal_status,
                "daily": [],
            }
        raw_rows = [dict(row) for row in rows]
        current_baseline_rows: List[Dict[str, object]] = []
        baseline_mismatch_rows: List[Dict[str, object]] = []
        legacy_baseline_rows: List[Dict[str, object]] = []
        promotion_ineligible_rows: List[Dict[str, object]] = []
        baseline_cache: Dict[str, str] = {}
        legacy_cache: Dict[str, str] = {}
        for row in raw_rows:
            row_strategy = strategy_name or str(row.get("strategy_name") or "")
            if row_strategy not in baseline_cache:
                baseline_cache[row_strategy] = str(validation_baseline_config(row_strategy).get("baseline_id") or "")
                legacy_cache[row_strategy] = legacy_validation_baseline_id(row_strategy)
            stored_baseline_id = _stored_validation_baseline_id(row.get("validation_baseline_id"), row_strategy)
            row["_stored_validation_baseline_id"] = stored_baseline_id
            row["_validation_baseline_matches_current"] = _matches_current_validation_baseline(
                stored_baseline_id,
                row_strategy,
                baseline_cache[row_strategy],
            )
            if stored_baseline_id == legacy_cache[row_strategy]:
                legacy_baseline_rows.append(row)
            if row["_validation_baseline_matches_current"] and bool(row.get("promotion_eligible", 1)):
                current_baseline_rows.append(row)
            elif row["_validation_baseline_matches_current"]:
                promotion_ineligible_rows.append(row)
            else:
                baseline_mismatch_rows.append(row)
        rows = current_baseline_rows
        window_rows = rows
        window_scope = "mixed" if current_version else "all"
        dates = []
        for row in window_rows:
            if row["signal_date"] not in dates:
                dates.append(row["signal_date"])
            if len(dates) >= days:
                break
        selected_all = [row for row in rows if row["signal_date"] in dates]
        base_cost = coerce_number(getattr(config, "VALIDATION_TRADE_COST_PCT", 0.25))
        if strategy_name:
            primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        else:
            primary_column, primary_days, primary_label = "strategy_primary_return", 0, "混合主周期"
        validation_baseline = validation_baseline_config(strategy_name)
        next_day_column = "signal_next_close_return" if strategy_name == "short_term" else "next_close_return"
        for row in selected_all:
            try:
                raw = json.loads(row.get("raw_json") or "{}")
            except Exception:
                raw = {}
            row["_raw"] = raw if isinstance(raw, dict) else {}
            row["_is_primary_signal"] = _is_primary_validation_signal(
                strategy_name or row["strategy_name"],
                row.get("rank"),
                row["_raw"],
            )
            row_primary_column, row_primary_days, row_primary_label = _primary_return_config(
                strategy_name or row["strategy_name"]
            )
            stored_primary_field = str(row.get("stored_primary_return_field") or "")
            has_stored_primary = stored_primary_field == row_primary_column
            row["_trade_cost_pct"] = _stored_or_current_trade_cost_pct(row)
            row["_primary_return"] = (
                coerce_number(row.get("stored_primary_return"))
                if has_stored_primary
                else coerce_number(row[row_primary_column])
            )
            row["_primary_return_net"] = (
                coerce_number(row.get("stored_primary_return_net"))
                if has_stored_primary
                else round(row["_primary_return"] - row["_trade_cost_pct"], 4)
            )
            row["_next_day_return_net"] = round(
                coerce_number(row.get(next_day_column)) - row["_trade_cost_pct"],
                4,
            )
            row["_next_day_return"] = coerce_number(row.get(next_day_column))
            uses_signal_entry = (strategy_name or row["strategy_name"]) == "short_term"
            drawdown_key = "signal_max_drawdown_3d" if uses_signal_entry else "open_max_drawdown_primary"
            row["_primary_drawdown"] = coerce_number(row.get(drawdown_key))
            row["_exit_return"] = coerce_number(
                row.get("signal_exit_return" if uses_signal_entry else "exit_return")
            )
            row["_exit_return_net"] = round(row["_exit_return"] - row["_trade_cost_pct"], 4)
            row["_intraday_high_return"] = coerce_number(
                row.get("signal_intraday_high_return" if uses_signal_entry else "open_intraday_high_return")
            )
            row["_hit_3pct"] = bool(row.get("signal_hit_3pct" if uses_signal_entry else "open_hit_3pct"))
            row["_hit_5pct"] = bool(row.get("signal_hit_5pct" if uses_signal_entry else "open_hit_5pct"))
            for holding_days in (3, 5, 10, 20):
                key = (
                    "signal_hold_{}d_return".format(holding_days)
                    if uses_signal_entry
                    else "hold_{}d_return".format(holding_days)
                )
                row["_hold_{}d_return".format(holding_days)] = coerce_number(row.get(key))
            row["_is_replay"] = _is_replay_version(row["strategy_version"])
            row["_survivorship_corrected"] = bool(row.get("survivorship_corrected"))
            row["_primary_ready"] = _outcome_ready(row, row_primary_days)
            row["_exit_ready"] = _outcome_ready(row, _exit_holding_days(strategy_name or row["strategy_name"]))
            row["_primary_holding_days"] = row_primary_days
            row["_primary_horizon_label"] = row_primary_label
        selected = [row for row in selected_all if row["_primary_ready"]]
        primary_rows = [row for row in selected if row["_is_primary_signal"]]
        primary_outcome_rows = [row for row in selected_all if row["_is_primary_signal"]]
        auxiliary_exit_rows = [row for row in primary_outcome_rows if row["_exit_ready"]]
        weak_next_day_exit_rows = [row for row in auxiliary_exit_rows if row["_next_day_return_net"] <= 0]
        replay_selected_all = [row for row in selected_all if row["_is_replay"]]
        real_rows = [row for row in primary_rows if not row["_is_replay"]]
        replay_rows = [row for row in primary_rows if row["_is_replay"]]
        real_primary_outcome_rows = [row for row in primary_outcome_rows if not row["_is_replay"]]
        replay_primary_outcome_rows = [row for row in primary_outcome_rows if row["_is_replay"]]
        survivor_primary_rows = [row for row in primary_rows if not row["_survivorship_corrected"]]
        survivorship_corrected_rows = [row for row in primary_rows if row["_survivorship_corrected"]]
        real_primary_ids = {id(row) for row in real_rows}
        replay_primary_ids = {id(row) for row in replay_rows}
        real_backup_rows = [
            row for row in selected if not row["_is_replay"] and id(row) not in real_primary_ids
        ]
        replay_backup_rows = [
            row for row in selected if row["_is_replay"] and id(row) not in replay_primary_ids
        ]
        real_daily = _daily_metrics(real_rows)
        replay_daily = _daily_metrics(replay_rows)
        top_k_sensitivity = _top_k_sensitivity(
            [row for row in selected if not row["_is_replay"]],
            getattr(config, "RESEARCH_TOP_K_SENSITIVITY", (3, 5, 10)),
        )
        real_daily_returns = [coerce_number(row.get("avg_primary_return_net")) for row in real_daily]
        real_return_ci = _mean_confidence_interval(real_daily_returns)
        real_win_ci_low = _wilson_lower_bound([value > 0 for value in real_daily_returns])
        primary_dates = []
        for row in primary_rows:
            if row["signal_date"] not in primary_dates:
                primary_dates.append(row["signal_date"])
        metrics = {
            "strategy_name": strategy_name,
            "strategy_version": current_version,
            "sample_count": len(primary_rows),
            "outcome_sample_count": len(primary_outcome_rows),
            "total_sample_count": len(selected),
            "total_outcome_sample_count": len(selected_all),
            "backup_sample_count": len(selected) - len(primary_rows),
            "backup_outcome_sample_count": len(selected_all) - len(primary_outcome_rows),
            "real_sample_count": len(real_rows),
            "replay_sample_count": len(replay_rows),
            "survivorship_corrected_count": len(survivorship_corrected_rows),
            "survivor_sample_count": len(survivor_primary_rows),
            "real_outcome_sample_count": len(real_primary_outcome_rows),
            "replay_outcome_sample_count": len(replay_primary_outcome_rows),
            "real_total_sample_count": len([row for row in selected if not row["_is_replay"]]),
            "replay_total_sample_count": len([row for row in selected if row["_is_replay"]]),
            "real_backup_sample_count": len(real_backup_rows),
            "replay_backup_sample_count": len(replay_backup_rows),
            "real_day_count": len(real_daily),
            "replay_day_count": len(replay_daily),
            "day_count": len(primary_dates),
            "outcome_day_count": len(dates),
            "primary_sample_scope": "real_only" if current_version and real_rows else "replay_only" if current_version and replay_rows else "all",
            "window_scope": window_scope,
            "primary_return_field": primary_column,
            "primary_holding_days": primary_days,
            "primary_horizon_label": primary_label,
            "validation_baseline": validation_baseline,
            "validation_baseline_id": validation_baseline["baseline_id"],
            "current_baseline_outcome_count": len(current_baseline_rows),
            "raw_outcome_sample_count": len(raw_rows),
            "legacy_baseline_outcome_count": len(legacy_baseline_rows),
            "excluded_baseline_mismatch_count": len(baseline_mismatch_rows),
            "excluded_promotion_ineligible_count": len(promotion_ineligible_rows),
            "trade_cost_pct": base_cost,
            "avg_trade_cost_pct": _avg(row["_trade_cost_pct"] for row in primary_outcome_rows),
            "avg_next_close_return": _avg(row["_next_day_return"] for row in primary_outcome_rows),
            "win_rate_next_close": _rate(row["_next_day_return"] > 0 for row in primary_outcome_rows),
            "hit_3pct_rate": _rate(row["_hit_3pct"] for row in primary_outcome_rows),
            "hit_5pct_rate": _rate(row["_hit_5pct"] for row in primary_outcome_rows),
            "avg_intraday_high_return": _avg(row["_intraday_high_return"] for row in primary_outcome_rows),
            "avg_hold_3d_return": _avg(row["_hold_3d_return"] for row in primary_rows),
            "avg_hold_5d_return": _avg(row["_hold_5d_return"] for row in primary_rows),
            "avg_hold_10d_return": _avg(row["_hold_10d_return"] for row in primary_rows),
            "avg_hold_20d_return": _avg(row["_hold_20d_return"] for row in primary_rows),
            "avg_primary_return": _avg(row["_primary_return"] for row in primary_rows),
            "avg_primary_return_net": _avg(row["_primary_return_net"] for row in primary_rows),
            "win_rate_primary": _rate(row["_primary_return"] > 0 for row in primary_rows),
            "win_rate_primary_net": _rate(row["_primary_return_net"] > 0 for row in primary_rows),
            "avg_primary_return_net_all": _avg(row["_primary_return_net"] for row in primary_rows),
            "win_rate_primary_net_all": _rate(row["_primary_return_net"] > 0 for row in primary_rows),
            "avg_primary_return_net_survivors": _avg(row["_primary_return_net"] for row in survivor_primary_rows),
            "win_rate_primary_net_survivors": _rate(row["_primary_return_net"] > 0 for row in survivor_primary_rows),
            "win_rate_all": _rate(row["_primary_return_net"] > 0 for row in primary_rows),
            "win_rate_survivors": _rate(row["_primary_return_net"] > 0 for row in survivor_primary_rows),
            "avg_next_day_return_net": _avg(row["_next_day_return_net"] for row in primary_outcome_rows),
            "win_rate_next_day_net": _rate(row["_next_day_return_net"] > 0 for row in primary_outcome_rows),
            "avg_1_5d_exit_return_net": _avg(row["_exit_return_net"] for row in auxiliary_exit_rows),
            "win_rate_1_5d_exit_net": _rate(row["_exit_return_net"] > 0 for row in auxiliary_exit_rows),
            "auxiliary_exit_sample_count": len(auxiliary_exit_rows),
            "positive_2_5d_after_weak_next_day_rate": _rate(
                row["_exit_return_net"] > 0 for row in weak_next_day_exit_rows
            ),
            "weak_next_day_1_5d_sample_count": len(weak_next_day_exit_rows),
            "avg_exit_return": _avg(row["_exit_return"] for row in auxiliary_exit_rows),
            "avg_exit_return_net": _avg(row["_exit_return_net"] for row in auxiliary_exit_rows),
            "win_rate_exit_net": _rate(row["_exit_return_net"] > 0 for row in auxiliary_exit_rows),
            "real_avg_primary_return_net": _avg(row["avg_primary_return_net"] for row in real_daily),
            "real_win_rate_primary_net": _rate(row["avg_primary_return_net"] > 0 for row in real_daily),
            "real_avg_primary_return_net_ci95_low": real_return_ci[0],
            "real_avg_primary_return_net_ci95_high": real_return_ci[1],
            "real_win_rate_primary_net_ci95_low": real_win_ci_low,
            "real_portfolio_max_drawdown_pct": _portfolio_max_drawdown(real_daily),
            "top_k_sensitivity": top_k_sensitivity,
            "replay_avg_primary_return_net": _avg(row["avg_primary_return_net"] for row in replay_daily),
            "replay_win_rate_primary_net": _rate(row["avg_primary_return_net"] > 0 for row in replay_daily),
            "avg_max_drawdown_3d": _avg(row["_primary_drawdown"] for row in primary_rows),
            "avg_max_drawdown_primary": _avg(row["_primary_drawdown"] for row in primary_rows),
            "real_avg_max_drawdown_primary": _avg(row["_primary_drawdown"] for row in real_rows),
            "top10_avg_next_close_return": _avg(
                row["_next_day_return"] for row in primary_outcome_rows if row["rank"] <= 10
            ),
            "avg_open_to_close_return": _avg(row["next_close_return"] for row in primary_outcome_rows),
            "next_day_compare": _next_day_compare(primary_outcome_rows),
            "replay_next_day_compare": _next_day_compare(replay_selected_all),
            "daily": _daily_metrics(primary_rows),
            "real_daily": real_daily,
            "replay_daily": replay_daily,
            "execution_skipped_count": execution_skipped_count,
            **signal_status,
        }
        return metrics

    def deepseek_attribution(self, strategy_name: str = "", days: int = 20) -> Dict[str, object]:
        strategy_name = str(strategy_name or "").strip()
        if not strategy_name:
            return {"status": "missing_strategy", "sample_count": 0, "days": int(days or 0)}
        primary_column, primary_days, primary_label = _primary_return_config(strategy_name)
        current_version = current_strategy_version(strategy_name)
        rows = self.repository.fetch_deepseek_attribution_rows(
            strategy_name,
            primary_column,
            current_version=current_version,
            replay_version=current_replay_strategy_version(strategy_name),
        )
        rows.sort(
            key=lambda row: (
                str(row["signal_date"] or ""),
                -int(coerce_number(row["rank"], 0) or 0),
            ),
            reverse=True,
        )
        if not rows:
            result = {
                "status": "empty",
                "strategy": strategy_name,
                "days": int(days or 0),
                "sample_count": 0,
                "primary_horizon_label": primary_label,
            }
            _write_deepseek_attribution_snapshot(strategy_name, result)
            return result

        selected_dates: List[str] = []
        for row in rows:
            date = row["signal_date"]
            if date not in selected_dates:
                selected_dates.append(date)
            if len(selected_dates) >= max(1, int(days)):
                break

        selected: List[Dict[str, object]] = []
        for row in rows:
            if row["signal_date"] not in selected_dates:
                continue
            item = dict(row)
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except Exception:
                raw = {}
            item["_raw"] = raw if isinstance(raw, dict) else {}
            item["_is_replay"] = _is_replay_version(item["strategy_version"])
            item["_primary_ready"] = int(item.get("future_days") or 1) >= primary_days
            item["_is_primary_signal"] = _is_primary_validation_signal(
                strategy_name,
                item.get("rank"),
                item["_raw"],
            )
            item["_trade_cost_pct"] = _execution_cost_pct(item)
            item["_primary_return"] = coerce_number(item.get("primary_return"))
            item["_primary_return_net"] = round(item["_primary_return"] - item["_trade_cost_pct"], 4)
            item["_deepseek_shadow_signal"] = bool(item.get("deepseek_shadow_signal"))
            selected.append(item)

        primary_rows = [row for row in selected if row["_primary_ready"]]
        primary_rows = [row for row in primary_rows if row["_is_primary_signal"]]
        attribution_rows = [row for row in primary_rows if _has_deepseek_review(row.get("_raw"))]
        real_rows = [row for row in attribution_rows if not row["_is_replay"]]
        replay_rows = [row for row in attribution_rows if row["_is_replay"]]
        covered_rows = [row for row in attribution_rows if _deepseek_covered(row.get("_raw"))]
        shadow_rows = [
            row
            for row in attribution_rows
            if row.get("_deepseek_shadow_signal") or bool((row.get("_raw") or {}).get("deepseek_shadow_filtered"))
        ]
        selected_rows = [row for row in attribution_rows if not row.get("_deepseek_shadow_signal")]
        avoid_veto_rows = [row for row in attribution_rows if _deepseek_avoid_or_veto(row.get("_raw"))]
        priority_rows = [row for row in attribution_rows if _deepseek_action(row.get("_raw")) == "priority"]
        watch_rows = [row for row in attribution_rows if _deepseek_action(row.get("_raw")) == "watch"]
        min_real_samples = 10
        status = "ok"
        if not attribution_rows:
            status = "no_deepseek_samples"
        elif len(real_rows) < min_real_samples:
            status = "insufficient_real_samples"
        counterfactual = _deepseek_counterfactual_topn(strategy_name, attribution_rows)
        priority_vs_watch = _deepseek_group_delta(priority_rows, watch_rows)
        token_cost = _deepseek_token_cost_summary(attribution_rows)
        token_value = _deepseek_token_value_metrics(
            avoid_veto_rows,
            counterfactual,
            token_cost,
        )
        budget_recommendation = _deepseek_budget_recommendation(
            status,
            len(real_rows),
            min_real_samples,
            token_value,
        )
        result = {
            "status": status,
            "strategy": strategy_name,
            "days": int(days or 0),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "primary_horizon_label": primary_label,
            "min_real_samples": min_real_samples,
            "sample_count": len(attribution_rows),
            "real_sample_count": len(real_rows),
            "replay_sample_count": len(replay_rows),
            "covered_sample_count": len(covered_rows),
            "selected_sample_count": len(selected_rows),
            "shadow_sample_count": len(shadow_rows),
            "covered_ratio_pct": round(len(covered_rows) / len(attribution_rows) * 100, 2) if attribution_rows else 0.0,
            "local_rank_sample_count": sum(1 for row in attribution_rows if _deepseek_local_rank(row) > 0),
            "reordered_sample_count": sum(
                1
                for row in attribution_rows
                if _deepseek_local_rank(row) > 0
                and _deepseek_shadow_rank(row) > 0
                and _deepseek_local_rank(row) != _deepseek_shadow_rank(row)
            ),
            "blend_alpha_avg": _avg(_deepseek_blend_alpha(row.get("_raw")) for row in attribution_rows if _deepseek_blend_alpha(row.get("_raw")) is not None),
            "avoid_veto": _return_summary(avoid_veto_rows),
            "shadow_avoid_veto": _return_summary([row for row in avoid_veto_rows if row.get("_deepseek_shadow_signal")]),
            "priority": _return_summary(priority_rows),
            "watch": _return_summary(watch_rows),
            "priority_vs_watch": priority_vs_watch,
            "counterfactual_topn": counterfactual,
            "token_cost": token_cost,
            "token_value": token_value,
            "value_per_1k_tokens": token_value.get("value_per_1k_tokens"),
            "budget_recommendation": budget_recommendation,
            "worth_expanding_budget": bool(budget_recommendation.get("worth_expanding_budget")),
            "notes": [
                "avoid/veto 包含正式入选与 DeepSeek gate 剔除后的 shadow 候选；正式策略胜率仍只按入选信号计算。",
            ],
        }
        _write_deepseek_attribution_snapshot(strategy_name, result)
        return result


class ValidationBaselineService:
    """Computes validation baseline status and backfill candidates."""

    def __init__(self, store) -> None:
        self.store = store
        self.repository = store.repository

    def status(self, strategy_name: str = "", days: int = 120) -> Dict[str, object]:
        strategy_name = str(strategy_name or "").strip()
        current_version = current_strategy_version(strategy_name)
        replay_version = current_replay_strategy_version(strategy_name)
        validation_baseline = validation_baseline_config(strategy_name)
        current_baseline_id = str(validation_baseline.get("baseline_id") or "")
        legacy_baseline_id_value = legacy_validation_baseline_id(strategy_name)
        primary_column, primary_days, primary_label = (
            _primary_return_config(strategy_name)
            if strategy_name
            else ("strategy_primary_return", 0, "混合主周期")
        )
        dates = self.repository.fetch_recent_signal_dates(
            strategy_name=strategy_name,
            current_version=current_version,
            replay_version=replay_version,
            days=days,
        )
        if not dates:
            min_oos_days = int(getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60))
            return {
                "strategy_name": strategy_name,
                "strategy_version": current_version,
                "days": int(days or 0),
                "status": "empty",
                "validation_baseline": validation_baseline,
                "validation_baseline_id": current_baseline_id,
                "legacy_baseline_id": legacy_baseline_id_value,
                "primary_return_field": primary_column,
                "primary_holding_days": primary_days,
                "primary_horizon_label": primary_label,
                "signal_count": 0,
                "raw_outcome_count": 0,
                "current_baseline_outcome_count": 0,
                "legacy_baseline_outcome_count": 0,
                "mismatched_baseline_outcome_count": 0,
                "pending_current_baseline_count": 0,
                "execution_skipped_count": 0,
                "current_baseline_day_count": 0,
                "current_primary_ready_count": 0,
                "current_primary_ready_day_count": 0,
                "promotion_ineligible_outcome_count": 0,
                "current_baseline_coverage_pct": None,
                "current_baseline_actionable_coverage_pct": None,
                "needs_backfill": False,
                "oos_ready": False,
                "min_oos_days": min_oos_days,
                "by_baseline": [],
                "date_range": {},
            }
        rows = self.repository.fetch_baseline_status_rows(
            dates,
            strategy_name=strategy_name,
            current_version=current_version,
            replay_version=replay_version,
        )

        signal_count = len(rows)
        raw_outcome_count = 0
        current_count = 0
        legacy_count = 0
        mismatch_count = 0
        skipped_count = 0
        pending_count = 0
        current_dates = set()
        current_primary_ready_count = 0
        current_primary_ready_dates = set()
        promotion_ineligible_count = 0
        by_baseline: Dict[str, Dict[str, object]] = {}
        baseline_cache: Dict[str, str] = {}
        legacy_cache: Dict[str, str] = {}
        for row in rows:
            row_strategy = strategy_name or str(row["strategy_name"] or "")
            if row_strategy not in baseline_cache:
                baseline_cache[row_strategy] = str(validation_baseline_config(row_strategy).get("baseline_id") or "")
                legacy_cache[row_strategy] = legacy_validation_baseline_id(row_strategy)
            has_outcome = bool(row["outcome_signal_id"])
            has_skip = bool(row["skip_signal_id"])
            promotion_eligible = bool(row["promotion_eligible"])
            if has_skip:
                skipped_count += 1
            if not has_outcome:
                if not has_skip:
                    pending_count += 1
                continue
            raw_outcome_count += 1
            stored_baseline_id = _stored_validation_baseline_id(row["validation_baseline_id"], row_strategy)
            matches_current = _matches_current_validation_baseline(
                stored_baseline_id,
                row_strategy,
                baseline_cache[row_strategy],
            )
            bucket = by_baseline.setdefault(
                stored_baseline_id,
                {
                    "baseline_id": stored_baseline_id,
                    "outcome_count": 0,
                    "day_count": 0,
                    "_dates": set(),
                    "is_current": matches_current,
                    "is_legacy": stored_baseline_id == legacy_cache[row_strategy],
                },
            )
            bucket["outcome_count"] = int(bucket["outcome_count"] or 0) + 1
            bucket["_dates"].add(str(row["signal_date"]))
            if stored_baseline_id == legacy_cache[row_strategy]:
                legacy_count += 1
            if matches_current:
                current_count += 1
                current_dates.add(str(row["signal_date"]))
                if not promotion_eligible:
                    promotion_ineligible_count += 1
                    continue
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except Exception:
                    raw = {}
                is_primary_signal = _is_primary_validation_signal(
                    row_strategy,
                    row["rank"],
                    raw if isinstance(raw, dict) else {},
                )
                if is_primary_signal and _outcome_ready(row, _primary_return_config(row_strategy)[1]):
                    current_primary_ready_count += 1
                    current_primary_ready_dates.add(str(row["signal_date"]))
            else:
                mismatch_count += 1
                if not has_skip:
                    pending_count += 1

        for bucket in by_baseline.values():
            bucket["day_count"] = len(bucket.pop("_dates"))
        by_baseline_rows = sorted(
            by_baseline.values(),
            key=lambda item: (-int(item.get("outcome_count") or 0), str(item.get("baseline_id") or "")),
        )
        coverage = round(current_count / signal_count * 100.0, 2) if signal_count else None
        actionable_coverage = (
            round((current_count + skipped_count) / signal_count * 100.0, 2)
            if signal_count
            else None
        )
        min_oos_days = int(getattr(config, "EXPECTED_RETURN_MIN_REAL_DAYS", 60))
        oos_ready = len(current_primary_ready_dates) >= min_oos_days
        needs_backfill = pending_count > 0 or mismatch_count > 0
        if needs_backfill:
            status = "needs_backfill"
        elif not oos_ready:
            status = "insufficient_current_baseline_samples"
        else:
            status = "ready_for_oos"
        return {
            "strategy_name": strategy_name,
            "strategy_version": current_version,
            "days": int(days or 0),
            "status": status,
            "validation_baseline": validation_baseline,
            "validation_baseline_id": current_baseline_id,
            "legacy_baseline_id": legacy_baseline_id_value,
            "primary_return_field": primary_column,
            "primary_holding_days": primary_days,
            "primary_horizon_label": primary_label,
            "signal_count": signal_count,
            "raw_outcome_count": raw_outcome_count,
            "current_baseline_outcome_count": current_count,
            "legacy_baseline_outcome_count": legacy_count,
            "mismatched_baseline_outcome_count": mismatch_count,
            "pending_current_baseline_count": pending_count,
            "execution_skipped_count": skipped_count,
            "current_baseline_day_count": len(current_dates),
            "current_primary_ready_count": current_primary_ready_count,
            "current_primary_ready_day_count": len(current_primary_ready_dates),
            "promotion_ineligible_outcome_count": promotion_ineligible_count,
            "current_baseline_coverage_pct": coverage,
            "current_baseline_actionable_coverage_pct": actionable_coverage,
            "needs_backfill": needs_backfill,
            "oos_ready": oos_ready,
            "min_oos_days": min_oos_days,
            "by_baseline": by_baseline_rows,
            "date_range": {"start": min(dates), "end": max(dates)} if dates else {},
        }

    def backfill_candidates(
        self,
        strategy_name: str,
        days: int = 120,
        limit: int = 500,
    ) -> Dict[str, object]:
        strategy_name = str(strategy_name or "").strip()
        if not strategy_name:
            return {"strategy_name": "", "candidate_count": 0, "codes": [], "dates": []}
        current_version = current_strategy_version(strategy_name)
        replay_version = current_replay_strategy_version(strategy_name)
        current_baseline_id = str(validation_baseline_config(strategy_name).get("baseline_id") or "")
        dates = self.repository.fetch_recent_signal_dates(
            strategy_name=strategy_name,
            current_version=current_version,
            replay_version=replay_version,
            days=days,
        )
        if not dates:
            return {"strategy_name": strategy_name, "candidate_count": 0, "codes": [], "dates": []}
        rows = self.repository.fetch_baseline_backfill_rows(
            dates,
            strategy_name,
            current_version=current_version,
            replay_version=replay_version,
            limit=limit,
        )
        candidates: List[Dict[str, object]] = []
        seen_codes = set()
        for row in rows:
            if row["skip_signal_id"]:
                continue
            has_current_outcome = bool(row["outcome_signal_id"]) and _matches_current_validation_baseline(
                row["validation_baseline_id"],
                strategy_name,
                current_baseline_id,
            )
            if has_current_outcome:
                continue
            code = normalize_code(row["code"])
            if not code or code in seen_codes:
                continue
            seen_codes.add(code)
            candidates.append(
                {
                    "code": code,
                    "name": row["name"] or code,
                    "latest_signal_date": row["signal_date"],
                    "best_rank": int(row["best_rank"] or 0),
                    "stored_baseline_id": _stored_validation_baseline_id(
                        row["validation_baseline_id"],
                        strategy_name,
                    )
                    if row["outcome_signal_id"]
                    else "",
                }
            )
            if len(candidates) >= max(1, int(limit)):
                break
        return {
            "strategy_name": strategy_name,
            "candidate_count": len(candidates),
            "codes": candidates,
            "dates": dates,
            "validation_baseline_id": current_baseline_id,
        }
