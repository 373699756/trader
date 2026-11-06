from __future__ import annotations

from dataclasses import replace

from datetime import date

from tests.helpers.training.research.application.historical_ports import TRADE_DATE, _summary


from trader.training.application.extraction import HistoricalExtractor


from trader.training.domain.evaluation.historical import ScoreComponent

from trader.training.domain.evaluation.specification import PREREGISTERED_RESEARCH_SPEC

from tests.helpers.training.research.application.historical_extraction import (
    _CoverageOnlyPort,
    _Evaluator,
    _Port,
    _WindowPort,
)


def test_extractor_is_deterministic_and_keeps_true_coverage_status() -> None:
    first = HistoricalExtractor(_Port(), _Evaluator()).extract()
    second = HistoricalExtractor(_Port(), _Evaluator()).extract()

    assert first.status == "exploratory"
    assert first.content_hash == second.content_hash
    assert tuple(item.summary.trade_date for item in first.days) == (TRADE_DATE,)
    assert first.coverage[-1].trade_date == TRADE_DATE
    assert first.coverage[-1].status == "valid"


def test_candidate_set_starts_from_each_board_top120_and_proves_exclusions() -> None:
    summary = _summary()
    candidates = tuple(
        replace(candidate, production_top120=candidate.code != "600001") for candidate in summary.candidates
    )
    result = HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()
    day = result.days[0]

    assert day.full_fields.requested_codes == ("300001", "600001", "688001")
    assert {proof.status for proof in day.proofs} == {"loaded"}
    assert all(len(proof.content_hash) == 64 for proof in day.proofs)


def test_candidate_set_excludes_candidate_blocked_by_selection_constraint() -> None:
    summary = _summary()
    candidates = tuple(
        replace(
            candidate,
            production_top120=candidate.code != "600001",
            final_components=(ScoreComponent("final", 1.0, 40.0),)
            if candidate.code == "600001"
            else candidate.final_components,
        )
        for candidate in summary.candidates
    )
    result = HistoricalExtractor(_Port(replace(summary, candidates=candidates)), _Evaluator()).extract()
    day = result.days[0]

    assert day.full_fields.requested_codes == ("300001", "688001")
    excluded = tuple(proof for proof in day.proofs if proof.code == "600001")
    assert {proof.status for proof in excluded} == {"excluded"}
    assert {proof.reason for proof in excluded} == {"selection_constraint"}


def test_missing_main_window_extends_back_only_to_preregistered_lower_bound() -> None:
    port = _CoverageOnlyPort()

    result = HistoricalExtractor(port, _Evaluator()).extract()

    assert result.status == "exploratory"
    assert result.days == ()
    assert date(2026, 6, 19) not in port.requested
    assert min(port.requested) == date(2026, 5, 18)
    assert max(port.requested) == date(2026, 8, 10)
    assert all(item.status == "failed" and item.reason == "point_in_time_data_missing" for item in result.coverage)


def test_extractor_stops_at_40_main_window_days_without_fallback() -> None:
    port = _WindowPort()

    result = HistoricalExtractor(port, _Evaluator()).extract()

    assert result.status == "extracted"
    assert len(result.days) == 40
    assert min(port.requested) == date(2026, 6, 15)
    assert date(2026, 6, 19) not in port.requested


def test_invalid_main_day_is_retained_and_replaced_only_by_nearest_prior_day() -> None:
    failed = date(2026, 7, 1)
    port = _WindowPort({failed})

    result = HistoricalExtractor(port, _Evaluator()).extract()

    assert result.status == "extracted"
    assert len(result.days) == 40
    assert min(day.summary.trade_date for day in result.days) == date(2026, 6, 12)
    assert any(item.trade_date == failed and item.status == "failed" for item in result.coverage)


def test_new_preregistered_window_never_replaces_a_failed_planned_day() -> None:
    failed = PREREGISTERED_RESEARCH_SPEC.historical_dates[4]
    port = _WindowPort({failed})

    result = HistoricalExtractor(port, _Evaluator(), spec=PREREGISTERED_RESEARCH_SPEC).extract()

    assert result.status == "exploratory"
    assert result.research_identity == "preregistered_research"
    assert result.research_spec_hash == PREREGISTERED_RESEARCH_SPEC.content_hash
    assert len(result.days) == 39
    assert tuple(port.requested) == PREREGISTERED_RESEARCH_SPEC.historical_dates
    assert any(item.trade_date == failed and item.status == "failed" for item in result.coverage)
