"""Coverage projections keep each head and the actual missing dates distinguishable."""

from datetime import date, timedelta

from trader.recommendation.domain.publication.models import Strategy
from trader.training.application.learned_rank_training import V1TrainingResult
from trader.training.application.profile_training import HeadTrainingResult, TrainingRunResult
from trader.training.domain.training_date_coverage import TrainingDateCoverage
from trader.training.domain.final_fit import BuildFinalFitDatePlan
from trader.training.entrypoints.profile_training import (
    ProjectLearnedRankTrainingResult, ProjectProfileTrainingResult, TrainingDateCoverageSummary,
)


def _coverage(strategy: Strategy, basis="sample_index") -> TrainingDateCoverage:
    return TrainingDateCoverage(
        strategy, "fitting", basis, date(2026, 11, 6), date(2026, 10, 30),
        (date(2026, 10, 27), date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30)),
        (date(2026, 10, 28),),
    )


def test_standard_projection_preserves_head_and_full_missing_dates():
    coverage = _coverage(Strategy.D25)
    head = HeadTrainingResult(
        Strategy.D25, "blocked", "complete_manifest", None, "a" * 64, 4, 4, "", "", 0, 0, 0,
        ("final_fit_effective_dates_insufficient",), date_coverage=(coverage,),
    )
    projected = ProjectProfileTrainingResult("v3", TrainingRunResult(None, "a" * 64, 0, (head,)))
    assert projected["status"] == "blocked"
    value = projected["heads"][0]["date_coverage"][0]
    assert value["strategy"] == "d25" and value["basis"] == "sample_index"
    assert value["expected_dates"] == 4 and value["effective_dates"] == 1
    assert value["missing_dates"] == ["2026-10-27", "2026-10-29", "2026-10-30"]
    assert value["source_cutoff"] == "2026-11-06" and value["label_cutoff"] == "2026-10-30"


def test_v1_metadata_block_is_not_an_engineering_failure_and_summary_is_bounded():
    coverage = _coverage(Strategy.TOMORROW, "metadata_upper_bound")
    result = V1TrainingResult("blocked", "", "a" * 64, 0, date_coverage=(coverage,))
    value = ProjectLearnedRankTrainingResult(result)
    assert value["status"] == "blocked" and value["heads"] == []
    assert value["date_coverage"][0]["missing_dates"] == ["2026-10-27", "2026-10-29", "2026-10-30"]
    summary = TrainingDateCoverageSummary((coverage,))
    assert "明日·诊断拟合 至多1/4日" in summary and "等3日" in summary
    assert "2026-10-30" not in summary
    assert TrainingDateCoverageSummary(()) == ""


def test_latest_observed_features_are_later_than_each_heads_mature_training_signal():
    cutoff = date(2026, 11, 6)
    calendar = tuple(day for offset in range(19) if (day := date(2026, 10, 19) + timedelta(days=offset)).weekday() < 5)
    tomorrow = BuildFinalFitDatePlan(calendar, source_cutoff=cutoff, fit_sessions=3, maturity_sessions=1)
    d25 = BuildFinalFitDatePlan(calendar, source_cutoff=cutoff, fit_sessions=3, maturity_sessions=5)
    assert tomorrow.source_cutoff == d25.source_cutoff == calendar[-1] == cutoff
    assert tomorrow.fit_dates[-1] == date(2026, 11, 5)
    assert d25.fit_dates[-1] == date(2026, 10, 30)
    assert tuple(day for day in calendar if d25.fit_dates[-1] < day <= cutoff)[1:] == (
        date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5), date(2026, 11, 6),
    )
