from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.specification import (
    ACTIVE_SCORE_RESEARCH_SPEC,
    SCORE_P0_V1_SPEC,
    SCORE_P0_V2_SPEC,
    ScoreResearchSpec,
    assess_score_research_coverage,
    get_score_research_spec,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=SHANGHAI)


def test_score_p0_v2_is_preregistered_before_its_complete_future_window() -> None:
    spec = SCORE_P0_V2_SPEC

    assert spec is ACTIVE_SCORE_RESEARCH_SPEC
    assert spec.research_identity == "score_p0_v2"
    assert spec.preregistered_on == date(2026, 8, 20)
    assert len(spec.historical_dates) == 40
    assert spec.historical_dates[0] == date(2026, 8, 21)
    assert spec.historical_dates[-1] == date(2026, 10, 23)
    assert spec.historical_replacement_dates == ()
    assert len(spec.forward_dates) == 20
    assert spec.forward_dates[0] == date(2026, 10, 26)
    assert spec.forward_dates[-1] == date(2026, 11, 20)
    assert spec.bootstrap_master_seed == 20260820
    assert len(spec.content_hash) == 64
    assert get_score_research_spec("score_p0_v1") is SCORE_P0_V1_SPEC
    assert get_score_research_spec("score_p0_v2") is SCORE_P0_V2_SPEC


def test_research_spec_rejects_registration_after_window_start() -> None:
    with pytest.raises(ValueError, match="before the first planned observation"):
        ScoreResearchSpec(
            research_identity="invalid",
            preregistered_on=date(2026, 8, 21),
            historical_dates=(date(2026, 8, 21),),
            historical_replacement_dates=(),
            forward_dates=(date(2026, 8, 22),),
            bootstrap_master_seed=1,
            maximum_historical_days=1,
        )


def test_future_research_coverage_fails_when_fixed_planned_dates_are_missed() -> None:
    coverage = assess_score_research_coverage(
        SCORE_P0_V2_SPEC,
        recorded_dates=(date(2026, 8, 21),),
        as_of=_at("2026-08-26T14:49:59"),
    )

    assert coverage.historical.state == "failed"
    assert coverage.historical.recorded_dates == (date(2026, 8, 21),)
    assert coverage.historical.missed_dates == (date(2026, 8, 24), date(2026, 8, 25))
    assert coverage.historical.maximum_attainable_days == 38
    assert coverage.historical.next_planned_date == date(2026, 8, 26)
    assert coverage.historical.complete is False
    assert coverage.historical.recoverable is False
    assert coverage.forward.state == "collecting"
    assert coverage.forward.missed_dates == ()
    assert coverage.forward.maximum_attainable_days == 20


def test_future_research_coverage_does_not_mark_today_or_future_dates_missed() -> None:
    coverage = assess_score_research_coverage(
        SCORE_P0_V2_SPEC,
        recorded_dates=(),
        as_of=_at("2026-08-21T14:49:59"),
    )

    assert coverage.historical.state == "collecting"
    assert coverage.historical.missed_dates == ()
    assert coverage.historical.next_planned_date == date(2026, 8, 21)
    assert coverage.historical.maximum_attainable_days == 40
    assert coverage.historical.recoverable is True


def test_future_research_coverage_marks_today_missed_at_the_fixed_cutoff() -> None:
    coverage = assess_score_research_coverage(
        SCORE_P0_V2_SPEC,
        recorded_dates=(date(2026, 8, 21),),
        as_of=_at("2026-08-26T14:50:00"),
    )

    assert coverage.historical.missed_dates == (
        date(2026, 8, 24),
        date(2026, 8, 25),
        date(2026, 8, 26),
    )
    assert coverage.historical.maximum_attainable_days == 37
    assert coverage.historical.next_planned_date == date(2026, 8, 27)


def test_research_coverage_rejects_naive_clock() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        assess_score_research_coverage(
            SCORE_P0_V2_SPEC,
            recorded_dates=(),
            as_of=datetime(2026, 8, 21, 14, 50),
        )


def test_research_coverage_is_complete_only_with_every_fixed_date() -> None:
    coverage = assess_score_research_coverage(
        SCORE_P0_V2_SPEC,
        recorded_dates=(*SCORE_P0_V2_SPEC.historical_dates, *SCORE_P0_V2_SPEC.forward_dates),
        as_of=_at("2026-11-21T00:00:00"),
    )

    assert coverage.historical.state == "complete"
    assert coverage.historical.complete is True
    assert coverage.historical.recoverable is True
    assert coverage.forward.state == "complete"
    assert coverage.forward.complete is True
