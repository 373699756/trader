"""Deterministic Score-R7 dossier assembly; never a production publisher."""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Sequence

from trader.application.research.score_r6 import evaluate_score_r6_forward
from trader.application.research.score_r6_models import ScoreR6ForwardDay, ScoreR6ForwardReport
from trader.application.research.score_r7_models import (
    ScoreR7GateResult,
    ScoreR7ParameterProposal,
    ScoreR7PromotionDossier,
    ScoreR7SampleCounts,
    ScoreR7SensitivityResult,
)
from trader.domain.research.score_r6 import ScoreR6ForwardSpec, ScoreR6ProductionCandidate

_COST_BPS = (20, 50, 100)
_BLOCK_DAYS = (3, 5, 10)
_REPETITIONS = 10_000


def build_score_r7_promotion_dossier(
    spec: ScoreR6ForwardSpec,
    days: Sequence[ScoreR6ForwardDay],
    report: ScoreR6ForwardReport,
    candidate: ScoreR6ProductionCandidate,
) -> ScoreR7PromotionDossier:
    """Recompute all source evidence and assemble a pending human-review dossier."""

    ordered = tuple(sorted(days, key=lambda item: item.trade_date))
    if not report.promotion_eligible or report.production_scope == "none":
        raise ValueError("Score-R7 requires a promotion-eligible Score-R6 report")
    if candidate.content_hash != spec.frozen_candidate_hash:
        raise ValueError("Score-R7 frozen candidate does not match the forward spec")
    recomputed = evaluate_score_r6_forward(spec, ordered)
    if recomputed.content_hash != report.content_hash:
        raise ValueError("Score-R7 recomputed forward report does not match the sealed report")
    if tuple(item.content_hash for item in ordered) != report.day_hashes:
        raise ValueError("Score-R7 recomputed day manifest does not match the sealed report")

    sensitivity = tuple(
        _sensitivity(spec.research_identity, ordered, cost_bps=cost_bps, block_days=block_days)
        for cost_bps in _COST_BPS
        for block_days in _BLOCK_DAYS
    )
    failed_dates = tuple(item.trade_date for item in ordered if item.status == "failed")
    proposal = ScoreR7ParameterProposal(
        candidate_hash=candidate.content_hash,
        component_names=candidate.boards[0].component_names,
        board_weight_units=tuple((item.board, item.weight_units) for item in candidate.boards),
        action_threshold=candidate.action_threshold,
        risk_penalty=candidate.risk_penalty,
    )
    if report.local_maximum_stock_weight is None or report.local_maximum_board_fraction is None:
        raise ValueError("Score-R7 eligible report is missing concentration evidence")
    local_mean = _required(report.local_mean_gain_pct, "local mean gain")
    local_severe = _required(report.local_severe_rate_delta, "local severe-rate delta")
    local_turnover = _required(report.local_turnover_delta, "local turnover delta")
    local_stability = _required(report.local_stability_delta, "local stability delta")
    local_recall = _required(report.local_recall, "local recall")
    hybrid_mean = _required(report.hybrid_mean_increment_pct, "hybrid mean increment")
    hybrid_lower = _required(report.hybrid_confidence_lower_pct, "hybrid confidence lower bound")
    hybrid_p_value = _required(report.hybrid_p_value, "hybrid p-value")
    hybrid_required = report.production_scope == "hybrid"
    return ScoreR7PromotionDossier(
        dossier_identity=f"{spec.research_identity}_promotion_dossier_v1",
        source_research_identity=spec.research_identity,
        historical_report_hash=spec.historical_report_hash,
        forward_spec_hash=spec.content_hash,
        forward_report_hash=report.content_hash,
        day_manifest_hashes=report.day_hashes,
        trading_calendar_hash=spec.trading_calendar_hash,
        rule_identity_hash=spec.rule_identity_hash,
        config_strategy_identity_hash=spec.config_strategy_identity_hash,
        data_schema_version=spec.data_schema_version,
        strategy_version=spec.strategy_version,
        fusion_version=spec.fusion_version,
        engine_version="score_r6_forward_gate_v1",
        statistical_program_version="score_r7_sensitivity_mbb_v1",
        production_scope=report.production_scope,
        proposed_parameters=proposal,
        sensitivity=sensitivity,
        gate_results=(
            ScoreR7GateResult(
                "hybrid_confidence_lower_pct", hybrid_lower, "greater_than", 0.0, hybrid_lower > 0.0, hybrid_required
            ),
            ScoreR7GateResult(
                "hybrid_mean_increment_pct",
                hybrid_mean,
                "at_least",
                spec.minimum_hybrid_increment_pct,
                hybrid_mean >= spec.minimum_hybrid_increment_pct,
                hybrid_required,
            ),
            ScoreR7GateResult(
                "hybrid_p_value",
                hybrid_p_value,
                "at_most",
                spec.bootstrap_alpha,
                hybrid_p_value <= spec.bootstrap_alpha,
                hybrid_required,
            ),
            ScoreR7GateResult(
                "local_maximum_board_fraction",
                report.local_maximum_board_fraction,
                "at_most",
                spec.maximum_local_board_fraction,
                report.local_maximum_board_fraction <= spec.maximum_local_board_fraction,
                True,
            ),
            ScoreR7GateResult(
                "local_maximum_stock_weight",
                report.local_maximum_stock_weight,
                "at_most",
                spec.maximum_local_stock_weight,
                report.local_maximum_stock_weight <= spec.maximum_local_stock_weight,
                True,
            ),
            ScoreR7GateResult(
                "local_mean_gain_pct",
                local_mean,
                "at_least",
                spec.minimum_local_gain_pct,
                local_mean >= spec.minimum_local_gain_pct,
                True,
            ),
            ScoreR7GateResult(
                "local_recall",
                local_recall,
                "at_least",
                spec.minimum_local_recall,
                local_recall >= spec.minimum_local_recall,
                True,
            ),
            ScoreR7GateResult(
                "local_severe_rate_delta",
                local_severe,
                "at_most",
                spec.maximum_local_severe_rate_delta,
                local_severe <= spec.maximum_local_severe_rate_delta,
                True,
            ),
            ScoreR7GateResult(
                "local_stability_delta",
                local_stability,
                "at_most",
                spec.maximum_local_stability_delta,
                local_stability <= spec.maximum_local_stability_delta,
                True,
            ),
            ScoreR7GateResult(
                "local_turnover_delta",
                local_turnover,
                "at_most",
                spec.maximum_local_turnover_delta,
                local_turnover <= spec.maximum_local_turnover_delta,
                True,
            ),
        ),
        failed_trade_dates=failed_dates,
        sample_counts=ScoreR7SampleCounts(
            planned_days=len(spec.planned_trade_dates),
            valid_days=sum(item.status in {"valid", "no_decision"} for item in ordered),
            failed_days=len(failed_dates),
            pair_count=report.pair_count,
        ),
        ablation_ids=("hybrid_vs_local", "local_vs_production"),
        maximum_stock_weight=report.local_maximum_stock_weight,
        maximum_board_fraction=report.local_maximum_board_fraction,
        residual_risks=("manual_review_required", "production_release_not_authorized"),
    )


def _sensitivity(
    research_identity: str,
    days: tuple[ScoreR6ForwardDay, ...],
    *,
    cost_bps: int,
    block_days: int,
) -> ScoreR7SensitivityResult:
    production = tuple(_portfolio_return(day, "production", cost_bps) for day in days)
    local = tuple(_portfolio_return(day, "local", cost_bps) for day in days)
    hybrid = tuple(_portfolio_return(day, "hybrid", cost_bps) for day in days)
    local_gains = tuple(value - baseline for value, baseline in zip(local, production, strict=True))
    hybrid_gains = tuple(value - baseline for value, baseline in zip(hybrid, local, strict=True))
    local_seed = _sensitivity_seed(research_identity, "local_vs_production", cost_bps, block_days)
    hybrid_seed = _sensitivity_seed(research_identity, "hybrid_vs_local", cost_bps, block_days)
    local_lower, local_upper, local_p_value = _moving_block_bootstrap(local_gains, block_days, local_seed)
    hybrid_lower, hybrid_upper, hybrid_p_value = _moving_block_bootstrap(hybrid_gains, block_days, hybrid_seed)
    return ScoreR7SensitivityResult(
        cost_bps=cost_bps,
        block_days=block_days,
        sample_days=len(days),
        local_mean_gain_pct=math.fsum(local_gains) / len(local_gains),
        local_confidence_lower_pct=local_lower,
        local_confidence_upper_pct=local_upper,
        local_p_value=local_p_value,
        local_bootstrap_seed=local_seed,
        hybrid_mean_increment_pct=math.fsum(hybrid_gains) / len(hybrid_gains),
        hybrid_confidence_lower_pct=hybrid_lower,
        hybrid_confidence_upper_pct=hybrid_upper,
        hybrid_p_value=hybrid_p_value,
        hybrid_bootstrap_seed=hybrid_seed,
    )


def _sensitivity_seed(research_identity: str, ablation_id: str, cost_bps: int, block_days: int) -> int:
    identity = f"{research_identity}|score_r7_sensitivity|{ablation_id}|{cost_bps}|{block_days}"
    return int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big", signed=False)


def _portfolio_return(day: ScoreR6ForwardDay, track: str, cost_bps: int) -> float:
    weights = tuple(getattr(pair, f"{track}_weight") for pair in day.pairs)
    gross = math.fsum(weight * pair.return_5d_pct for weight, pair in zip(weights, day.pairs, strict=True))
    return gross - cost_bps / 100.0 if math.fsum(weights) > 0.0 else 0.0


def _required(value: float | None, label: str) -> float:
    if value is None:
        raise ValueError(f"Score-R7 eligible report is missing {label}")
    return value


def _moving_block_bootstrap(values: tuple[float, ...], block_days: int, seed: int) -> tuple[float, float, float]:
    if len(values) < block_days:
        raise ValueError("Score-R7 sensitivity sample is shorter than its block")
    observed = math.fsum(values) / len(values)
    centered = tuple(value - observed for value in values)
    rng = random.Random(seed)
    samples: list[float] = []
    extreme = 0
    block_starts = len(values) - block_days + 1
    for _index in range(_REPETITIONS):
        indices: list[int] = []
        while len(indices) < len(values):
            start = rng.randrange(block_starts)
            indices.extend(range(start, start + block_days))
        indices = indices[: len(values)]
        samples.append(math.fsum(values[index] for index in indices) / len(values))
        null_mean = math.fsum(centered[index] for index in indices) / len(values)
        extreme += null_mean >= observed
    samples.sort()
    return (
        samples[math.ceil(0.025 * len(samples)) - 1],
        samples[math.ceil(0.975 * len(samples)) - 1],
        (extreme + 1) / (_REPETITIONS + 1),
    )


__all__ = ["build_score_r7_promotion_dossier"]
