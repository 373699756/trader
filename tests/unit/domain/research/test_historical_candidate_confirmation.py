from datetime import date, timedelta

from trader.domain.research.filter_recall_ablation import FilterAblationRow, run_filter_recall_ablation
from trader.domain.research.historical_candidate_confirmation import CandidateConfirmationSeries, confirm_transparent_candidates
from trader.domain.research.transparent_candidate import preregister_transparent_candidates


def test_confirmation_keeps_terminal_holdout_closed_and_joint_family():
    dates = tuple(date(2025, 1, 1) + timedelta(days=index) for index in range(10))
    rows = tuple(
        FilterAblationRow(
            day, f"60{index:04d}", "main", "industry", True, False, True, True, True, 80.0, index + 1, 0.01, 0.007, False
        )
        for index, day in enumerate(dates)
    )
    family = preregister_transparent_candidates(run_filter_recall_ablation(rows, strategy="tomorrow", development_dates=dates))
    series = tuple(
        CandidateConfirmationSeries(candidate.candidate_id, dates, tuple(0.01 if candidate.change_kind != "control" else 0.0 for _ in dates), tuple(0.008 if candidate.change_kind != "control" else 0.0 for _ in dates), tuple(0.0 for _ in dates))
        for candidate in family.candidates
    )
    report = confirm_transparent_candidates(family, series, repetitions=100)
    assert report.terminal_holdout_status == "terminal_holdout_not_opened"
    assert report.production_authority is False
    assert report.status in {"historical_candidate_ready", "historical_rejected"}
