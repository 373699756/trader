"""Public fields for offline company replay diagnostics."""

from __future__ import annotations

from trader.training.application.company_replay import CompanyReplayReport
from trader.training.domain.evaluation.company_replay import CompanyReplaySeatMetrics
from trader.training.domain.evaluation.selection_experiment import SELECTION_COST_BPS


def ProjectCompanyReplayReport(report: CompanyReplayReport) -> dict[str, object]:
    metrics = report.metrics
    return {
        "status": report.status,
        "evidence_kind": report.evidence_kind,
        "profile": report.profile,
        "strategy": report.strategy.value if report.strategy is not None else None,
        "target_bps": report.target_bps,
        "failure_reasons": report.failure_reasons,
        "production_authority": False,
        "quality_accepted": False,
        "terminal_holdout_opened": False,
        "diagnostics": None if metrics is None else {
            "dates": tuple(day.isoformat() for day in metrics.trade_dates),
            "cost_scenarios_bps": SELECTION_COST_BPS,
            "seats": [_seat_metrics(item) for item in metrics.seats],
            "paired_effects": [
                {
                    "comparison": item.comparison,
                    "seat_group": item.seat_group,
                    "paired_days": item.paired_days,
                    "mean_six_slot_net_excess_delta": item.mean_six_slot_net_excess_delta,
                }
                for item in metrics.effects
            ],
            "risk_gate_effects": [
                {
                    "comparison": item.comparison,
                    "newly_rejected_seats": item.newly_rejected_seats,
                    "known_profit_outcomes": item.known_profit_outcomes,
                    "profitable_at_20bp": item.profitable_at_20bp,
                    "known_risk_outcomes": item.known_risk_outcomes,
                    "severe_losses": item.severe_losses,
                    "reason_counts": dict(item.reason_counts),
                }
                for item in metrics.risk_effects
            ],
        },
    }


def _seat_metrics(item: CompanyReplaySeatMetrics) -> dict[str, object]:
    return {
        "arm": item.arm,
        "seat_group": item.seat_group,
        "seats": item.seats,
        "empty_days": item.empty_days,
        "known_targets": item.known_targets,
        "hits": item.hits,
        "known_risk_outcomes": item.known_risk_outcomes,
        "severe_losses": item.severe_losses,
        "known_entries": item.known_entries,
        "untradable_entries": item.untradable_entries,
        "known_exits": item.known_exits,
        "untradable_exits": item.untradable_exits,
        "known_return_days": item.known_return_days,
        "mean_six_slot_net_excess_returns": item.mean_six_slot_net_excess_returns,
    }


__all__ = ["ProjectCompanyReplayReport"]
