from __future__ import annotations

from dataclasses import replace

import pytest

from tests.helpers.training.research.application.historical_extraction import _Evaluator, _Port, _WindowPort

from tests.helpers.training.research.application.historical_ports import _summary


from trader.training.application.extraction import HistoricalExtractor

from trader.training.application.replay import HistoricalBaselineReplayer

from tests.helpers.training.research.application.historical_replay import (
    _InvalidReplayEvaluator,
    _NoFreezeEvaluator,
    _RecallEvaluator,
    _ReplayEvaluator,
)


def test_r3_refuses_to_claim_a_baseline_report_without_40_valid_days() -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()

    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)

    assert report.status == "exploratory"
    assert report.day_count == 1
    assert report.cost_rates == (0.002, 0.005, 0.01)
    assert report.report_hash == HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction).report_hash


def test_r3_marks_exactly_40_valid_days_replayed() -> None:
    extraction = HistoricalExtractor(_WindowPort(), _Evaluator()).extract()

    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)

    assert report.status == "replayed"
    assert report.day_count == 40


def test_r3_report_calculates_cost_mae_recall_coverage_concentration_and_rank_ic() -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)
    day = report.days[0]

    assert day.selected_codes == ("300001", "600001")
    assert day.oracle_codes == ("300001", "600001")
    assert day.selection_status == "selected"
    assert day.net_excess_returns == pytest.approx((0.0296, 0.029, 0.028))
    assert day.mean_mae_atr20 == -0.5
    assert day.severe_drawdown_rate == 0.0
    assert day.candidate_recall == 1.0
    assert (day.evaluated_count, day.oracle_selected_count, day.recalled_oracle_count) == (3, 2, 2)
    assert day.field_coverage == 0.0
    assert day.maximum_board_fraction == 0.5
    assert day.maximum_industry_fraction == 1.0
    assert day.rank_ic is None
    assert report.aggregate.net_excess_returns == pytest.approx((0.0296, 0.029, 0.028))


def test_r3_candidate_recall_uses_the_candidate_set_oracle_micro_denominator() -> None:
    summary = _summary()
    candidates = tuple(replace(item, production_top120=item.code != "688001") for item in summary.candidates)
    extraction = HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()

    report = HistoricalBaselineReplayer(_RecallEvaluator()).replay(extraction)

    assert report.days[0].candidate_recall == 0.5
    assert report.days[0].oracle_codes == ("300001", "688001")
    assert report.aggregate.candidate_recall == 0.5


def test_r3_records_legal_no_freeze_as_zero_exposure() -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()

    report = HistoricalBaselineReplayer(_NoFreezeEvaluator()).replay(extraction)

    day = report.days[0]
    assert day.selection_status == "no_freeze"
    assert day.selected_codes == ()
    assert day.net_excess_returns == (0.0, 0.0, 0.0)
    assert day.mean_mae_atr20 is None


def test_r3_rejects_non_contiguous_replay_ranks() -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()

    with pytest.raises(ValueError, match="contiguous"):
        HistoricalBaselineReplayer(_InvalidReplayEvaluator()).replay(extraction)
