"""Public whitelist for isolated eleven-layer research validation reports."""

from __future__ import annotations

from trader.training.application.selection_execution import SelectionResearchExecution
from trader.training.domain.evaluation.selection_experiment import SelectionExperimentResult
from trader.training.domain.evaluation.selection_statistics import SelectionInference


def ProjectSelectionValidation(execution: SelectionResearchExecution) -> dict[str, object]:
    spec, report = execution.specification, execution.report
    return {
        "schema_version": "full_population_selection_validation",
        "research_identity": spec.research_identity,
        "status": report.status,
        "specification_hash": spec.content_hash,
        "prerequisite_hash": execution.prerequisite_hash,
        "report_hash": report.content_hash,
        "profile_id": spec.profile_id,
        "strategy": spec.strategy,
        "parents": {
            "dataset_hash": spec.dataset_hash,
            "dataset_manifest_hash": spec.dataset_manifest_hash,
            "candidate_recall_hash": spec.candidate_recall_hash,
            "configuration_hash": spec.configuration_hash,
            "cost_reference_hash": spec.cost_reference_hash,
            "industry_model_hash": spec.industry_model_hash,
            "pooled_ridge_model_hash": spec.pooled_ridge_model_hash,
            "pooled_lightgbm_model_hash": spec.pooled_lightgbm_model_hash,
        },
        "partition": spec.partition,
        "partition_hash": spec.partition_hash,
        "date_split_hash": spec.date_split.content_hash,
        "research_dates": [value.isoformat() for value in spec.research_dates],
        "selection_contract": {
            "pipeline_stages": list(spec.pipeline_stages),
            "population": "full_eligible_population",
            "score": "risk_adjusted_local_score_round_half_up_2dp",
            "top_k": spec.top_k,
            "maximum_per_board": spec.maximum_board_count,
            "board_rounding": "ceil_6_times_0_60",
            "maximum_per_industry": None,
            "veto_refill": False,
            "action_refill": False,
            "production_variant": "rule_model_gate_model_gate_then_fixed_top6",
            "production_target": "tomorrow" if spec.strategy == "tomorrow" else "mean_t2_t5",
            "cost_bps": list(spec.cost_bps),
            "cost_application": "once_per_horizon",
        },
        "statistics_plan": {
            "unit": "equal_weight_parent_trade_date",
            "method": "moving_block_bootstrap",
            "block_days": spec.bootstrap_block_days,
            "repetitions": spec.bootstrap_repetitions,
            "master_seed": spec.bootstrap_master_seed,
            "confidence": 0.95,
            "holm_alpha": 0.05,
            "holm_family": "all_non_control_variants_times_20_50bp",
            "stress_cost_bps": 100,
            "selected_mean_unit": "selected_stock_observation",
            "cash_slot_contribution": 0,
        },
        "failure_reasons": list(report.failure_reasons),
        "results": [_result(item) for item in report.results],
        "inference": [_inference(item) for item in execution.inference],
        "historical_acceptance": "not_granted",
        "acceptance_blockers": [
            "confirmation_risk_capacity_ranking_gates_required",
            "terminal_holdout_not_opened",
            "shadow_not_completed",
            "manual_production_authorization_missing",
        ],
        "production_authority": False,
        "automatic_model_update": False,
        "terminal_holdout_opened": False,
    }


def _result(result: SelectionExperimentResult) -> dict[str, object]:
    variant, metrics = result.variant, result.metrics
    return {
        "arm": variant.arm,
        "selection_mode": variant.selection_mode,
        "target": variant.target,
        "pooled_model_family": variant.pooled_model_family,
        "evaluated_trade_dates": metrics.evaluated_trade_dates,
        "population_rows": metrics.population_rows,
        "selected_rows": metrics.selected_rows,
        "empty_portfolio_days": metrics.empty_portfolio_days,
        "mean_six_slot_net_excess_returns": list(metrics.mean_six_slot_net_excess_returns),
        "mean_selected_net_excess_returns": list(metrics.mean_selected_net_excess_returns),
        "paired_control_increments": list(metrics.paired_control_increments),
        "horizon_mean_net_excess_returns_20bp": list(metrics.horizon_mean_net_excess_returns_20bp),
        "rank_ic": metrics.rank_ic,
        "ndcg_at_6": metrics.ndcg_at_6,
        "win_rate_20bp": metrics.win_rate_20bp,
        "maximum_six_slot_drawdown_20bp": metrics.maximum_drawdown_20bp,
        "turnover": metrics.turnover,
        "severe_loss_rate": metrics.severe_loss_rate,
        "mean_mae_atr20": metrics.mean_mae_atr20,
        "exit_untradable_rate": metrics.exit_untradable_rate,
        "mean_execution_capacity": metrics.mean_execution_capacity,
        "capacity_shortfall_rate": metrics.capacity_shortfall_rate,
        "selected_oracle_top6_recall": metrics.selected_oracle_top6_recall,
        "days": [
            {
                "trade_date": day.trade_date.isoformat(),
                "fixed_count": len(day.fixed_codes),
                "selected_count": len(day.selected_codes),
                "six_slot_net_excess_returns": list(day.six_slot_net_excess_returns),
                "selected_mean_net_excess_returns": list(day.selected_mean_net_excess_returns),
                "horizon_six_slot_returns": [list(values) for values in day.horizon_six_slot_returns],
            }
            for day in result.days
        ],
    }


def _inference(item: SelectionInference) -> dict[str, object]:
    statistics = item.statistics
    return {
        "variant_id": item.variant_id,
        "cost_bps": item.cost_bps,
        "valid": statistics.valid,
        "invalid_reason": statistics.invalid_reason,
        "sample_count": statistics.sample_count,
        "seed": statistics.seed,
        "observed_mean": statistics.observed_mean,
        "confidence_lower": statistics.confidence_lower,
        "confidence_upper": statistics.confidence_upper,
        "paired_control_mean": statistics.paired_metric_observed_mean,
        "paired_control_confidence_lower": statistics.paired_metric_confidence_lower,
        "paired_control_confidence_upper": statistics.paired_metric_confidence_upper,
        "p_value": statistics.p_value,
        "holm_threshold": item.holm.threshold,
        "holm_rank": item.holm.family_rank,
        "holm_rejected_null": item.holm.rejected_null,
    }


__all__ = ["ProjectSelectionValidation"]
