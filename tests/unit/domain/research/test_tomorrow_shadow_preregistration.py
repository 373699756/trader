from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from trader.domain.research.specification import SCORE_P0_V1_SPEC, SCORE_P0_V2_SPEC
from trader.domain.research.tomorrow_shadow_preregistration import (
    TOMORROW_SHADOW_CHALLENGER_FAMILY,
    TOMORROW_SHADOW_P1_SPEC,
    TomorrowShadowCalendarAttestation,
)


def test_tomorrow_shadow_p1_freezes_a_new_non_overlapping_40_plus_20_window() -> None:
    spec = TOMORROW_SHADOW_P1_SPEC

    assert spec.research_identity == "score_tomorrow_shadow_p1_v1"
    assert spec.preregistered_on == date(2026, 8, 28)
    assert len(spec.historical_dates) == 40
    assert spec.historical_dates[0] == date(2027, 6, 14)
    assert spec.historical_dates[-1] == date(2027, 8, 6)
    assert len(spec.forward_dates) == 20
    assert spec.forward_dates[0] == date(2027, 8, 9)
    assert spec.forward_dates[-1] == date(2027, 9, 3)
    assert not set((*spec.historical_dates, *spec.forward_dates)) & set(
        (
            *SCORE_P0_V1_SPEC.historical_dates,
            *SCORE_P0_V1_SPEC.historical_replacement_dates,
            *SCORE_P0_V1_SPEC.forward_dates,
            *SCORE_P0_V2_SPEC.historical_dates,
            *SCORE_P0_V2_SPEC.forward_dates,
        )
    )
    assert spec.challenger_family == TOMORROW_SHADOW_CHALLENGER_FAMILY
    assert spec.challengers[-1].model_families == ("linear", "lightgbm")
    assert spec.challengers[-1].model_weights == (0.5, 0.5)
    assert spec.cost_rates == (0.002, 0.005, 0.01)
    assert spec.bootstrap_block_days == (3, 5, 10)
    assert spec.primary_block_days == 5
    assert spec.bootstrap_repetitions == 10_000
    assert spec.minimum_total_pairs == 300
    assert spec.minimum_forward_pairs == 100
    assert spec.minimum_mean_increment == 0.002
    assert spec.production_authority is False


def test_calendar_attestation_must_confirm_every_frozen_date_before_collection() -> None:
    spec = TOMORROW_SHADOW_P1_SPEC
    attestation = TomorrowShadowCalendarAttestation(
        research_spec_hash=spec.content_hash,
        confirmed_on=date(2026, 12, 31),
        authority_document_hash="a" * 64,
        trading_dates=(*spec.historical_dates, *spec.forward_dates),
    )

    assert attestation.trading_dates == (*spec.historical_dates, *spec.forward_dates)
    assert len(attestation.content_hash) == 64
    with pytest.raises(ValueError, match="exactly match"):
        replace(attestation, trading_dates=attestation.trading_dates[:-1])
    with pytest.raises(ValueError, match="before the first planned date"):
        replace(attestation, confirmed_on=spec.historical_dates[0])


def test_preregistration_rejects_a_sixth_challenger_or_threshold_change() -> None:
    spec = TOMORROW_SHADOW_P1_SPEC

    with pytest.raises(ValueError, match="fixed five-challenger family"):
        replace(spec, challengers=(*spec.challengers, spec.challengers[-1]))
    with pytest.raises(ValueError, match="frozen promotion thresholds"):
        replace(spec, minimum_oracle_recall=0.98)
    with pytest.raises(ValueError, match=r"fixed 40\+20 dates"):
        replace(spec, historical_dates=())
