from __future__ import annotations

from dataclasses import replace

import pytest

from tests.unit.application.research.test_historical_ports import _summary
from tests.unit.application.research.test_score_r2_extraction import _Evaluator, _Port
from trader.application.research.challenger_models import ChallengerReplaySelection
from trader.application.research.challengers import ScoreR4ChallengerReplayer
from trader.application.research.extraction import ScoreR2HistoricalExtractor
from trader.application.research.replay import ScoreR3BaselineReplayer
from trader.application.research.replay_models import BaselineReplaySelection
from trader.application.research.score_r5 import ScoreR5FinalSealer, ScoreR5ForwardCollector, ScoreR5StatisticalGate


class _ChallengerEvaluator:
    def replay(self, day, specification, overrides):  # noqa: ANN001
        summary = {item.code: item for item in day.summary.candidates}
        baseline = tuple(item for item in day.evaluated if summary[item.code].production_top120)[:2]
        baseline_ranks = {item.code: rank for rank, item in enumerate(baseline, start=1)}
        selectable = tuple(
            item
            for item, override in zip(day.evaluated, overrides, strict=True)
            if override.selection_eligible and not override.force_observe_only
        )
        local_ranks = {item.code: rank for rank, item in enumerate(selectable[:2], start=1)}
        return tuple(
            ChallengerReplaySelection(
                item.code,
                baseline_ranks.get(item.code),
                local_ranks.get(item.code),
                local_ranks.get(item.code),
                item.local_score,
                item.local_score if summary[item.code].recorded_deepseek_score is None else 70.0,
                "control_copy" if summary[item.code].recorded_deepseek_score is None else "existing_facts",
            )
            for item in day.evaluated
        )


class _R4BaselineEvaluator:
    def replay(self, day):  # noqa: ANN001
        summary = {item.code: item for item in day.summary.candidates}
        production = tuple(item for item in day.evaluated if summary[item.code].production_top120)[:2]
        ranks = {item.code: rank for rank, item in enumerate(production, start=1)}
        return tuple(
            BaselineReplaySelection(item.code, ranks.get(item.code), ranks.get(item.code)) for item in day.evaluated
        )


def _inputs():
    summary = _summary()
    candidates = tuple(
        replace(
            item,
            production_top120=item.code != "600001",
            recorded_deepseek_score=65.0 if item.code == "300001" else None,
        )
        for item in summary.candidates
    )
    extraction = ScoreR2HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()
    baseline = ScoreR3BaselineReplayer(_R4BaselineEvaluator()).replay(extraction)
    return extraction, baseline


def test_r4_replays_all_five_variants_as_same_day_same_stock_pairs() -> None:
    extraction, baseline = _inputs()

    report = ScoreR4ChallengerReplayer(_ChallengerEvaluator()).replay(extraction, baseline)

    assert report.status == "exploratory"
    assert tuple(item.variant_id for item in report.variants) == (
        "continuous_entry",
        "coverage_shrink",
        "candidate_upper_bound",
        "heat_weak_structure",
        "combined_v1",
    )
    assert report.deepseek_http_request_delta == 0
    assert len(report.parameter_manifest_hash) == 64
    for variant in report.variants:
        day = variant.days[0]
        assert tuple(item.code for item in day.pairs) == ("300001", "600001", "688001")
        assert all(item.settlement.decision_date == day.trade_date for item in day.pairs)
        assert sum(item.production_weight for item in day.pairs) == pytest.approx(1.0)
        assert sum(item.local_weight for item in day.pairs) in (pytest.approx(0.0), pytest.approx(1.0))
        assert sum(item.hybrid_weight for item in day.pairs) in (pytest.approx(0.0), pytest.approx(1.0))
    assert (
        report.content_hash
        == ScoreR4ChallengerReplayer(_ChallengerEvaluator()).replay(extraction, baseline).content_hash
    )


def test_candidate_upper_bound_alone_can_select_a_loaded_non_top120_candidate() -> None:
    extraction, baseline = _inputs()

    report = ScoreR4ChallengerReplayer(_ChallengerEvaluator()).replay(extraction, baseline)

    by_id = {item.variant_id: item for item in report.variants}
    ordinary = {item.code: item for item in by_id["coverage_shrink"].days[0].overrides}
    expanded = {item.code: item for item in by_id["candidate_upper_bound"].days[0].overrides}
    assert ordinary["600001"].selection_eligible is False
    assert expanded["600001"].active_set_expanded is True
    assert expanded["600001"].selection_eligible is True


class _ManufacturedFactsEvaluator(_ChallengerEvaluator):
    def replay(self, day, specification, overrides):  # noqa: ANN001
        rows = super().replay(day, specification, overrides)
        return (rows[0], replace(rows[1], hybrid_source="existing_facts"), *rows[2:])


def test_r4_rejects_hybrid_facts_that_were_not_recorded_in_r2() -> None:
    extraction, baseline = _inputs()

    with pytest.raises(ValueError, match="existing facts"):
        ScoreR4ChallengerReplayer(_ManufacturedFactsEvaluator()).replay(extraction, baseline)


def test_r5_exploratory_r4_evidence_terminates_every_variant_before_forward() -> None:
    extraction, baseline = _inputs()
    challengers = ScoreR4ChallengerReplayer(_ChallengerEvaluator()).replay(extraction, baseline)

    report = ScoreR5StatisticalGate().evaluate(baseline, challengers)

    assert report.status == "exploratory"
    assert report.historical_day_count == 1
    assert tuple(item.variant_id for item in report.variants) == (
        "continuous_entry",
        "coverage_shrink",
        "candidate_upper_bound",
        "heat_weak_structure",
        "combined_v1",
    )
    assert all(item.state == "historical_rejected" for item in report.variants)
    assert all("historical_day_count" in item.failure_reasons for item in report.variants)
    assert all(item.local_track.cost_mean_differences[0] is not None for item in report.variants)
    assert report.deepseek_http_request_delta == 0

    collector = ScoreR5ForwardCollector(report)
    with pytest.raises(ValueError, match="historical gate"):
        collector.record_failed("continuous_entry", report.forward_dates[0], "source_unavailable")

    final = ScoreR5FinalSealer().seal(report, baseline, challengers, ())
    assert final.state == "forward_rejected"
    assert final.failure_reasons == ("no_historical_variant_passed",)
    assert len(report.forward_dates) == 20
    assert report.forward_dates[0].isoformat() == "2026-11-02"
    assert report.forward_dates[-1].isoformat() == "2026-11-27"


def test_r5_forward_collector_is_append_only_after_a_frozen_pass_identity() -> None:
    extraction, baseline = _inputs()
    challengers = ScoreR4ChallengerReplayer(_ChallengerEvaluator()).replay(extraction, baseline)
    exploratory = ScoreR5StatisticalGate().evaluate(baseline, challengers)
    passed = replace(exploratory.variants[0], state="historical_passed", failure_reasons=())
    frozen = replace(exploratory, variants=(passed, *exploratory.variants[1:]))
    collector = ScoreR5ForwardCollector(frozen)
    planned_date = frozen.forward_dates[0]

    first = collector.record_failed("continuous_entry", planned_date, "source_unavailable")
    assert collector.record_failed("continuous_entry", planned_date, "source_unavailable") == first
    assert collector.records("continuous_entry") == (first,)
    with pytest.raises(ValueError, match="identity conflict"):
        collector.record_failed("continuous_entry", planned_date, "service_stopped")
