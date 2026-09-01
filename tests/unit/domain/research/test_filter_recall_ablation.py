from datetime import date, timedelta

from trader.domain.research.filter_recall_ablation import FilterAblationRow, run_filter_recall_ablation
from trader.domain.research.transparent_candidate import evaluate_transparent_candidate, preregister_transparent_candidates


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


def test_transparent_family_contains_control_and_never_authorizes_production():
    report = run_filter_recall_ablation(_rows(), strategy="tomorrow", development_dates=tuple(row.trade_date for row in _rows()))
    family = preregister_transparent_candidates(report)
    assert family.candidates[0].change_kind == "control"
    assert len(family.candidates) <= 8
    assert family.production_authority is False
    assert evaluate_transparent_candidate(family.candidates[0], _rows()).candidate_id.endswith("control")
