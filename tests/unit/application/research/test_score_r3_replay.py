from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.application.research.test_historical_ports import _summary
from tests.unit.application.research.test_score_r2_extraction import _Evaluator, _Port, _WindowPort
from trader.application.research.extraction import ScoreR2HistoricalExtractor
from trader.application.research.replay import ScoreR3BaselineReplayer
from trader.application.research.replay_models import BaselineReplaySelection


class _ReplayEvaluator:
    def replay(self, day):  # noqa: ANN001
        return tuple(
            BaselineReplaySelection(item.code, rank if rank <= 2 else None, rank if rank <= 2 else None)
            for rank, item in enumerate(sorted(day.evaluated, key=lambda value: value.code), start=1)
        )


def test_r3_refuses_to_claim_a_baseline_report_without_40_valid_days() -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()

    report = ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(extraction)

    assert report.status == "exploratory"
    assert report.day_count == 1
    assert report.cost_rates == (0.002, 0.005, 0.01)
    assert report.report_hash == ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(extraction).report_hash


def test_r3_marks_exactly_40_valid_days_replayed() -> None:
    extraction = ScoreR2HistoricalExtractor(_WindowPort(), _Evaluator()).extract()

    report = ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(extraction)

    assert report.status == "replayed"
    assert report.day_count == 40


def test_r3_report_calculates_cost_mae_recall_coverage_concentration_and_rank_ic() -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(extraction)
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


class _RecallEvaluator(_ReplayEvaluator):
    def replay(self, day):  # noqa: ANN001
        ordered = sorted(day.evaluated, key=lambda value: value.code)
        return (
            BaselineReplaySelection(ordered[0].code, 1, 1),
            BaselineReplaySelection(ordered[1].code, 2, None),
            BaselineReplaySelection(ordered[2].code, None, 2),
        )


def test_r3_candidate_recall_uses_the_active_set_oracle_micro_denominator() -> None:
    summary = _summary()
    candidates = tuple(replace(item, production_top120=item.code != "688001") for item in summary.candidates)
    extraction = ScoreR2HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()

    report = ScoreR3BaselineReplayer(_RecallEvaluator()).replay(extraction)

    assert report.days[0].candidate_recall == 0.5
    assert report.days[0].oracle_codes == ("300001", "688001")
    assert report.aggregate.candidate_recall == 0.5


class _NoDecisionEvaluator(_ReplayEvaluator):
    def replay(self, day):  # noqa: ANN001
        return tuple(BaselineReplaySelection(item.code, None, None) for item in day.evaluated)


def test_r3_records_legal_no_decision_as_zero_exposure() -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()

    report = ScoreR3BaselineReplayer(_NoDecisionEvaluator()).replay(extraction)

    day = report.days[0]
    assert day.selection_status == "no_decision"
    assert day.selected_codes == ()
    assert day.net_excess_returns == (0.0, 0.0, 0.0)
    assert day.mean_mae_atr20 is None


class _InvalidReplayEvaluator(_ReplayEvaluator):
    def replay(self, day):  # noqa: ANN001
        result = super().replay(day)
        return (replace(result[0], production_rank=2), replace(result[1], production_rank=2), result[2])


def test_r3_rejects_non_contiguous_replay_ranks() -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()

    with pytest.raises(ValueError, match="contiguous"):
        ScoreR3BaselineReplayer(_InvalidReplayEvaluator()).replay(extraction)
