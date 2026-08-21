from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.application.research.score_r6 import evaluate_score_r6_forward
from trader.application.research.score_r6_models import ScoreR6ForwardDay, ScoreR6ForwardPair
from trader.application.research.score_r7 import build_score_r7_promotion_dossier
from trader.domain.research.score_r6 import (
    ScoreR6ForwardSpec,
    ScoreR6ProductionBoardWeights,
    ScoreR6ProductionCandidate,
)


def _candidate() -> ScoreR6ProductionCandidate:
    names = ("tail_structure", "turnover_flow", "trend", "stability", "market_state", "entry_quality")
    return ScoreR6ProductionCandidate(
        "a" * 64,
        tuple(
            ScoreR6ProductionBoardWeights(board, names, (1667, 556, 2222, 2777, 1111, 1667))
            for board in ("main", "chinext", "star")
        ),
        78,
        4,
    )


def _spec(candidate: ScoreR6ProductionCandidate) -> ScoreR6ForwardSpec:
    registered = date(2026, 12, 1)
    planned: list[date] = []
    current = registered
    while len(planned) < 20:
        current += timedelta(days=1)
        if current.weekday() < 5:
            planned.append(current)
    return ScoreR6ForwardSpec(
        research_identity="score_r6_forward_20261201_v1",
        preregistered_on=registered,
        planned_trade_dates=tuple(planned),
        historical_report_hash="1" * 64,
        frozen_candidate_hash=candidate.content_hash,
        trading_calendar_hash="3" * 64,
        rule_identity_hash="4" * 64,
        config_strategy_identity_hash="5" * 64,
    )


def _eligible_days(spec: ScoreR6ForwardSpec, *, hybrid_better: bool = True) -> tuple[ScoreR6ForwardDay, ...]:
    days = []
    for trade_date in spec.planned_trade_dates:
        pairs = tuple(
            ScoreR6ForwardPair(
                code=f"60{index:04d}",
                board=("main", "chinext", "star")[index % 3],
                production_weight=0.2 if index < 5 else 0.0,
                local_weight=0.2 if 5 <= index < 10 else 0.0,
                hybrid_weight=0.2 if (10 <= index < 15 if hybrid_better else 5 <= index < 10) else 0.0,
                return_5d_pct=float(index),
                severe_loss=False,
            )
            for index in range(15)
        )
        days.append(
            ScoreR6ForwardDay(
                research_spec_hash=spec.content_hash,
                trade_date=trade_date,
                status="valid",
                pairs=pairs,
                oracle_codes=tuple(f"60{index:04d}" for index in range(5, 10)),
                failure_reason=None,
            )
        )
    return tuple(days)


def test_r7_dossier_recomputes_eligible_evidence_and_stays_pending_manual_review() -> None:
    candidate = _candidate()
    spec = _spec(candidate)
    days = _eligible_days(spec)
    report = evaluate_score_r6_forward(spec, days, minimum_pair_count=100)

    dossier = build_score_r7_promotion_dossier(spec, days, report, candidate)

    assert dossier.schema_version == "score_r7_promotion_dossier_v1"
    assert dossier.manual_review_status == "pending"
    assert dossier.production_change_authorized is False
    assert dossier.production_scope == "hybrid"
    assert dossier.forward_report_hash == report.content_hash
    assert dossier.day_manifest_hashes == report.day_hashes
    assert dossier.proposed_parameters.component_names == (
        "tail_structure",
        "turnover_flow",
        "trend",
        "stability",
        "market_state",
        "entry_quality",
    )
    assert {(item.cost_bps, item.block_days) for item in dossier.sensitivity} == {
        (cost, block) for cost in (20, 50, 100) for block in (3, 5, 10)
    }
    assert all(item.local_confidence_lower_pct <= item.local_confidence_upper_pct for item in dossier.sensitivity)
    assert all(item.local_bootstrap_seed != item.hybrid_bootstrap_seed for item in dossier.sensitivity)
    assert dossier.sample_counts.planned_days == 20
    assert dossier.sample_counts.failed_days == 0
    assert dossier.ablation_ids == ("hybrid_vs_local", "local_vs_production")
    assert tuple(item.gate_id for item in dossier.gate_results) == (
        "hybrid_confidence_lower_pct",
        "hybrid_mean_increment_pct",
        "hybrid_p_value",
        "local_maximum_board_fraction",
        "local_maximum_stock_weight",
        "local_mean_gain_pct",
        "local_recall",
        "local_severe_rate_delta",
        "local_stability_delta",
        "local_turnover_delta",
    )
    assert all(item.passed for item in dossier.gate_results)
    assert "manual_review_required" in dossier.residual_risks


def test_r7_dossier_rejects_noneligible_or_nonreproducible_r6_evidence() -> None:
    candidate = _candidate()
    spec = _spec(candidate)
    days = _eligible_days(spec)
    eligible = evaluate_score_r6_forward(spec, days, minimum_pair_count=100)
    collecting = evaluate_score_r6_forward(spec, days[:-1], minimum_pair_count=100)

    with pytest.raises(ValueError, match="promotion-eligible"):
        build_score_r7_promotion_dossier(spec, days[:-1], collecting, candidate)
    with pytest.raises(ValueError, match="recomputed"):
        build_score_r7_promotion_dossier(
            spec, days, replace(eligible, day_hashes=tuple(reversed(eligible.day_hashes))), candidate
        )
    with pytest.raises(ValueError, match="candidate"):
        build_score_r7_promotion_dossier(spec, days, eligible, replace(candidate, action_threshold=76))


def test_r7_dossier_keeps_unproved_hybrid_out_of_a_local_only_scope() -> None:
    candidate = _candidate()
    spec = _spec(candidate)
    days = _eligible_days(spec, hybrid_better=False)
    report = evaluate_score_r6_forward(spec, days, minimum_pair_count=100)

    dossier = build_score_r7_promotion_dossier(spec, days, report, candidate)

    assert dossier.production_scope == "local_only"
    hybrid_gates = tuple(item for item in dossier.gate_results if item.gate_id.startswith("hybrid_"))
    assert all(item.required_for_scope is False for item in hybrid_gates)
    assert not all(item.passed for item in hybrid_gates)
