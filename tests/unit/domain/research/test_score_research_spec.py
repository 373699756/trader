from __future__ import annotations

from datetime import date

import pytest

from trader.domain.research.specification import (
    ACTIVE_SCORE_RESEARCH_SPEC,
    SCORE_P0_V1_SPEC,
    SCORE_P0_V2_SPEC,
    ScoreResearchSpec,
    get_score_research_spec,
)


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
