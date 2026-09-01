from datetime import date, timedelta

import pytest

from trader.domain.research.filter_recall_ablation import (
    FilterAblationRow,
    FilterScoreComponent,
    run_filter_recall_ablation,
)
from trader.domain.research.transparent_candidate import (
    TransparentCandidate,
    evaluate_transparent_candidate,
    preregister_transparent_candidates,
)


def _rows():
    return tuple(
        FilterAblationRow(
            trade_date=date(2025, 1, 1) + timedelta(days=index),
            code=f"60{index:04d}",
            board="main",
            industry=f"industry-{index % 2}",
            permanent_eligible=True,
            safety_veto=False,
            evidence_complete=index != 0,
            candidate_present=True,
            candidate_reliable=True,
            candidate_score=80.0,
            candidate_rank=index + 1,
            actual_net_excess_20bp=0.01,
            actual_net_excess_50bp=0.007,
            severe_loss=False,
        )
        for index in range(10)
    )


def test_ablation_preserves_controls_and_is_hash_stable():
    rows = _rows()
    dates = tuple(row.trade_date for row in rows)
    first = run_filter_recall_ablation(rows, strategy="tomorrow", development_dates=dates)
    second = run_filter_recall_ablation(rows, strategy="tomorrow", development_dates=dates)
    assert first.content_hash == second.content_hash
    assert first.baseline_recall == 0.9
    assert first.production_authority is False
    assert len(first.contributions) == 5
    evidence = next(item for item in first.contributions if item.rule == "evidence")
    assert evidence.first_blocked_count == 1
    assert evidence.exclusive_blocked_count == 1
    assert evidence.recall_delta_20bp == pytest.approx(0.1)
    assert evidence.recall_delta_50bp == pytest.approx(0.1)
    assert evidence.classification == "observe_candidate"
    assert first.recommendations == ("evidence",)


def test_transparent_family_contains_control_and_never_authorizes_production():
    report = run_filter_recall_ablation(
        _rows(), strategy="tomorrow", development_dates=tuple(row.trade_date for row in _rows())
    )
    family = preregister_transparent_candidates(report)
    assert family.candidates[0].change_kind == "control"
    assert len(family.candidates) <= 8
    assert family.production_authority is False
    assert evaluate_transparent_candidate(family.candidates[0], _rows()).candidate_id.endswith("control")


def test_candidate_missing_and_unknown_score_are_not_conflated_with_zero():
    row = _rows()[0]

    with pytest.raises(ValueError, match="present candidate requires"):
        FilterAblationRow(**{**row.__dict__, "candidate_score": None})

    missing = FilterAblationRow(
        **{
            **row.__dict__,
            "evidence_complete": True,
            "candidate_present": False,
            "candidate_score": None,
            "candidate_rank": None,
        }
    )
    assert missing.matched_rules == ("candidate_missing",)


def test_severe_loss_interception_and_resource_savings_use_fixed_population():
    rows = list(_rows())
    rows[0] = FilterAblationRow(
        **{
            **rows[0].__dict__,
            "evidence_complete": False,
            "actual_net_excess_20bp": -0.05,
            "actual_net_excess_50bp": -0.053,
            "severe_loss": True,
            "io_requests": 3,
            "scoring_rows": 1,
        }
    )
    report = run_filter_recall_ablation(
        tuple(rows), strategy="tomorrow", development_dates=tuple(row.trade_date for row in rows)
    )
    evidence = next(item for item in report.contributions if item.rule == "evidence")
    assert report.baseline_severe_loss_interception == 1.0
    assert evidence.severe_loss_interception == 0.0
    assert evidence.io_saved == 3
    assert evidence.scoring_rows_saved == 1


def test_severe_loss_guard_uses_point_in_time_risk_not_outcome_label():
    rows = list(_rows())
    rows[0] = FilterAblationRow(
        **{
            **rows[0].__dict__,
            "evidence_complete": True,
            "severe_loss": True,
            "predicted_severe_loss_risk": 0.05,
        }
    )
    candidate = TransparentCandidate("tomorrow_cost_guard", "tomorrow", "cost_guard", severe_loss_guard=0.10)

    metrics = evaluate_transparent_candidate(candidate, tuple(rows))

    assert metrics.evaluated_rows == 1
    assert metrics.severe_loss_rate == 1.0


def test_removed_component_recomputes_and_renormalizes_candidate_score():
    row = FilterAblationRow(
        date(2025, 1, 1),
        "600001",
        "main",
        "industry",
        True,
        False,
        True,
        True,
        True,
        47.5,
        1,
        0.01,
        0.007,
        False,
        score_components=(
            FilterScoreComponent("weak", 40.0, 0.25),
            FilterScoreComponent("strong", 50.0, 0.75),
        ),
    )
    control = TransparentCandidate("tomorrow_control", "tomorrow", "control")
    candidate = TransparentCandidate(
        "tomorrow_remove_weak",
        "tomorrow",
        "remove_component",
        removed_component="weak",
    )

    assert evaluate_transparent_candidate(control, (row,)).evaluated_rows == 0
    assert evaluate_transparent_candidate(candidate, (row,)).evaluated_rows == 1
