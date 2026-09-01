from dataclasses import replace
from datetime import date, timedelta

from trader.domain.research.filter_recall_ablation import FilterAblationRow, run_filter_recall_ablation
from trader.domain.research.historical_candidate_confirmation import (
    CandidateConfirmationSeries,
    confirm_transparent_candidates,
)
from trader.domain.research.transparent_candidate import preregister_transparent_candidates


def test_confirmation_keeps_terminal_holdout_closed_and_joint_family():
    dates = tuple(date(2025, 1, 1) + timedelta(days=index) for index in range(10))
    rows = tuple(
        FilterAblationRow(
            day,
            f"60{index:04d}",
            "main",
            "industry",
            True,
            False,
            True,
            True,
            True,
            80.0,
            index + 1,
            0.01,
            0.007,
            False,
        )
        for index, day in enumerate(dates)
    )
    family = preregister_transparent_candidates(
        run_filter_recall_ablation(rows, strategy="tomorrow", development_dates=dates)
    )
    series = tuple(
        CandidateConfirmationSeries(
            candidate.candidate_id,
            dates,
            tuple(0.01 if candidate.change_kind != "control" else 0.0 for _ in dates),
            tuple(0.008 if candidate.change_kind != "control" else 0.0 for _ in dates),
            tuple(0.0 for _ in dates),
            candidate_net_excess_20bp=tuple(0.02 for _ in dates),
            candidate_net_excess_50bp=tuple(0.017 for _ in dates),
            development_fold_directions=(1, 1, 1, 1, 1),
        )
        for candidate in family.candidates
    )
    selected_candidate_id = family.candidates[1].candidate_id
    confirmation_dates = tuple(day + timedelta(days=20) for day in dates)
    confirmation_series = tuple(
        replace(item, trade_dates=confirmation_dates, development_fold_directions=())
        for item in series
        if item.candidate_id in {family.candidates[0].candidate_id, selected_candidate_id}
    )
    report = confirm_transparent_candidates(
        family,
        series,
        confirmation_series=confirmation_series,
        selected_candidate_id=selected_candidate_id,
        repetitions=100,
    )
    assert report.terminal_holdout_status == "terminal_holdout_not_opened"
    assert report.production_authority is False
    assert report.status in {"historical_candidate_ready", "historical_rejected"}


def test_confirmation_rejects_positive_increment_with_negative_absolute_returns():
    dates = tuple(date(2025, 1, 1) + timedelta(days=index) for index in range(10))
    rows = tuple(
        FilterAblationRow(
            day,
            f"60{index:04d}",
            "main",
            "industry",
            True,
            False,
            True,
            True,
            True,
            80.0,
            index + 1,
            0.01,
            0.007,
            False,
        )
        for index, day in enumerate(dates)
    )
    family = preregister_transparent_candidates(
        run_filter_recall_ablation(rows, strategy="tomorrow", development_dates=dates)
    )
    series = tuple(
        CandidateConfirmationSeries(
            candidate.candidate_id,
            dates,
            tuple(0.01 for _ in dates),
            tuple(0.008 for _ in dates),
            tuple(0.0 for _ in dates),
            candidate_net_excess_20bp=tuple(-0.01 for _ in dates),
            candidate_net_excess_50bp=tuple(-0.013 for _ in dates),
            development_fold_directions=(1, 1, 1, 1, 1),
        )
        for candidate in family.candidates
    )

    selected_candidate_id = family.candidates[1].candidate_id
    confirmation_dates = tuple(day + timedelta(days=20) for day in dates)
    confirmation_series = tuple(
        replace(item, trade_dates=confirmation_dates, development_fold_directions=())
        for item in series
        if item.candidate_id in {family.candidates[0].candidate_id, selected_candidate_id}
    )
    report = confirm_transparent_candidates(
        family,
        series,
        confirmation_series=confirmation_series,
        selected_candidate_id=selected_candidate_id,
        repetitions=100,
    )

    assert report.status == "historical_rejected"
    assert any("absolute_20bp_not_positive" in item.failure_reasons for item in report.evidence[1:])


def test_confirmation_retains_additional_filter_candidates_in_the_joint_holm_family():
    dates = tuple(date(2025, 1, 1) + timedelta(days=index) for index in range(10))
    rows = tuple(
        FilterAblationRow(
            day,
            f"60{index:04d}",
            "main",
            "industry",
            True,
            False,
            True,
            True,
            True,
            80.0,
            index + 1,
            0.01,
            0.007,
            False,
        )
        for index, day in enumerate(dates)
    )
    family = preregister_transparent_candidates(
        run_filter_recall_ablation(rows, strategy="tomorrow", development_dates=dates)
    )
    family_series = tuple(
        CandidateConfirmationSeries(
            candidate.candidate_id,
            dates,
            tuple(0.0 if candidate.change_kind == "control" else 0.01 for _ in dates),
            tuple(0.0 if candidate.change_kind == "control" else 0.008 for _ in dates),
            tuple(0.0 for _ in dates),
            candidate_net_excess_20bp=tuple(0.02 for _ in dates),
            candidate_net_excess_50bp=tuple(0.017 for _ in dates),
            development_fold_directions=(1, 1, 1, 1, 1),
        )
        for candidate in family.candidates
    )
    filter_series = CandidateConfirmationSeries(
        "tomorrow_filter_evidence_observe",
        dates,
        tuple(0.006 for _ in dates),
        tuple(0.004 for _ in dates),
        tuple(0.0 for _ in dates),
        candidate_net_excess_20bp=tuple(0.016 for _ in dates),
        candidate_net_excess_50bp=tuple(0.013 for _ in dates),
        development_fold_directions=(1, 1, 1, 1, 1),
    )

    selected_candidate_id = family.candidates[1].candidate_id
    confirmation_dates = tuple(day + timedelta(days=20) for day in dates)
    confirmation_series = tuple(
        replace(item, trade_dates=confirmation_dates, development_fold_directions=())
        for item in family_series
        if item.candidate_id in {family.candidates[0].candidate_id, selected_candidate_id}
    )
    report = confirm_transparent_candidates(
        family,
        family_series,
        confirmation_series=confirmation_series,
        additional_series=(filter_series,),
        selected_candidate_id=selected_candidate_id,
        repetitions=100,
    )

    assert report.holm_family[-1] == filter_series.candidate_id
    assert tuple(item.candidate_id for item in report.evidence) == report.holm_family
    assert report.selected_candidate_id != filter_series.candidate_id
