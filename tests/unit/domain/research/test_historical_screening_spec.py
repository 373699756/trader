from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC


def test_score_h0_v1_preregisters_a_retrospective_screen_without_promotion_authority() -> None:
    spec = SCORE_H0_V1_SPEC

    assert spec.research_identity == "score_h0_v1"
    assert spec.registered_on == date(2026, 8, 20)
    assert spec.source_cutoff == date(2026, 8, 19)
    assert spec.download_sessions == 640
    assert spec.training_start == date(2024, 7, 1)
    assert spec.training_end == date(2025, 12, 31)
    assert spec.validation_start == date(2026, 1, 1)
    assert spec.validation_end == date(2026, 7, 31)
    assert spec.minimum_history_sessions == 61
    assert spec.label_horizon_sessions == 5
    assert spec.round_trip_cost_bps == 20
    assert spec.promotion_authority is False
    assert len(spec.content_hash) == 64


def test_score_h0_spec_rejects_overlap_and_future_source_data() -> None:
    with pytest.raises(ValueError, match="training window"):
        replace(SCORE_H0_V1_SPEC, training_end=date(2026, 2, 1))
    with pytest.raises(ValueError, match="source cutoff"):
        replace(SCORE_H0_V1_SPEC, source_cutoff=date(2026, 7, 31))
