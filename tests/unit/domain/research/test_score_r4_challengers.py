from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from trader.domain.research.challengers import (
    R4_PARAMETER_SET_VERSION,
    ContinuousEntryInputs,
    HeatWeakStructureInputs,
    assess_continuous_entry,
    assess_heat_weak_structure,
    challenger_parameter_manifest,
    challenger_registry,
)


def test_r4_registry_has_five_independent_immutable_versions() -> None:
    variants = challenger_registry()

    assert tuple(item.variant_id for item in variants) == (
        "continuous_entry",
        "coverage_shrink",
        "candidate_upper_bound",
        "heat_weak_structure",
        "combined",
    )
    assert tuple(item.variant_version for item in variants) == (
        "continuous_entry",
        "coverage_shrink_baseline",
        "candidate_upper_bound",
        "heat_weak_structure",
        "combined",
    )
    assert all(item.parameter_set_version == R4_PARAMETER_SET_VERSION for item in variants)
    assert len({id(item) for item in variants}) == 5
    combined = variants[-1]
    assert (
        combined.continuous_entry,
        combined.coverage_shrink,
        combined.candidate_upper_bound,
        combined.heat_weak_structure,
    ) == (True, True, True, True)
    with pytest.raises(FrozenInstanceError):
        variants[0].continuous_entry = False  # type: ignore[misc]


def test_r4_machine_manifest_contains_every_preregistered_endpoint() -> None:
    manifest = challenger_parameter_manifest()

    assert tuple(
        (item.name, item.direction, item.lower_endpoint, item.production_threshold, item.upper_endpoint)
        for item in manifest.entry_transitions
    ) == (
        ("ma5_ma10_spread_pct", "higher", -0.5, 0.0, 0.5),
        ("ma10_ma20_spread_pct", "higher", -0.5, 0.0, 0.5),
        ("ma20_slope_pct", "higher", -0.2, 0.0, 0.2),
        ("price_ma5_distance_pct", "lower", 0.8, 1.0, 1.2),
        ("price_ma10_distance_pct", "lower", 1.6, 2.0, 2.4),
        ("pullback_volume_to_5d_average", "lower", 0.6, 0.7, 0.8),
        ("price_ma20_spread_pct", "higher", -0.5, 0.0, 0.5),
        ("price_prior_high_20d_spread_pct", "higher", -0.5, 0.0, 0.5),
        ("breakout_volume_to_5d_average", "higher", 1.8, 2.0, 2.2),
        ("close_location", "higher", 65.0, 70.0, 75.0),
        ("breakout_deviation_pct", "lower", 4.0, 5.0, 6.0),
    )
    assert tuple((item.board, item.lower_inclusive, item.hard_cap_inclusive) for item in manifest.heat_bands) == (
        ("main", 6.0, 8.0),
        ("chinext", 12.0, 16.0),
        ("star", 12.0, 16.0),
    )
    assert (
        manifest.weak_structure.close_location_maximum,
        manifest.weak_structure.tail_return_30m_pct_maximum,
        manifest.weak_structure.intraday_drawdown_pct_minimum,
    ) == (35.0, -0.5, 3.0)


def test_continuous_entry_uses_preregistered_centered_memberships() -> None:
    pullback = assess_continuous_entry(
        ContinuousEntryInputs(
            price=10.0,
            ma5=10.0,
            ma10=10.0,
            ma20=10.0,
            ma20_slope_pct=0.0,
            volume_to_5d_average=0.70,
            prior_high_20d=10.0,
            breakout_deviation_pct=5.0,
            close_location=70.0,
        )
    )
    breakout = assess_continuous_entry(
        ContinuousEntryInputs(
            price=10.0,
            ma5=10.0,
            ma10=10.0,
            ma20=10.0,
            ma20_slope_pct=0.0,
            volume_to_5d_average=2.0,
            prior_high_20d=10.0,
            breakout_deviation_pct=5.0,
            close_location=70.0,
        )
    )

    assert pullback.status == "scored"
    assert pullback.pullback_score == pytest.approx(50.0)
    assert pullback.score == pytest.approx(50.0)
    assert breakout.status == "scored"
    assert breakout.breakout_score == pytest.approx(50.0)
    assert breakout.score == pytest.approx(50.0)


def test_continuous_entry_keeps_independent_required_fields_and_fails_closed() -> None:
    complete_breakout = assess_continuous_entry(
        ContinuousEntryInputs(
            price=10.1,
            ma5=None,
            ma10=None,
            ma20=None,
            ma20_slope_pct=None,
            volume_to_5d_average=2.3,
            prior_high_20d=10.0,
            breakout_deviation_pct=3.0,
            close_location=80.0,
        )
    )
    unknown_maximum = assess_continuous_entry(
        ContinuousEntryInputs(
            price=10.0,
            ma5=None,
            ma10=None,
            ma20=None,
            ma20_slope_pct=None,
            volume_to_5d_average=2.0,
            prior_high_20d=10.0,
            breakout_deviation_pct=5.0,
            close_location=70.0,
        )
    )

    assert complete_breakout.status == "scored"
    assert complete_breakout.score == 100.0
    assert unknown_maximum.status == "critical_missing"
    assert unknown_maximum.score is None


@pytest.mark.parametrize(
    ("board", "change_pct"),
    (("main", 6.0), ("main", 8.0), ("chinext", 12.0), ("star", 16.0)),
)
def test_heat_weak_structure_uses_inclusive_board_bands(board: str, change_pct: float) -> None:
    assessment = assess_heat_weak_structure(
        board,
        HeatWeakStructureInputs(
            change_pct=change_pct,
            close_location=34.0,
            tail_return_30m_pct=0.0,
            intraday_drawdown_pct=0.0,
        ),
    )

    assert assessment.in_high_heat_band is True
    assert assessment.force_observe_only is True
    assert assessment.reasons == ("weak_close",)


def test_heat_without_weak_structure_does_not_observe_and_hard_reject_identity_is_refused() -> None:
    strong = assess_heat_weak_structure(
        "main",
        HeatWeakStructureInputs(7.0, 80.0, 0.2, 1.0),
    )

    assert strong.force_observe_only is False
    assert strong.reasons == ()
    with pytest.raises(ValueError, match="hard heat cap"):
        assess_heat_weak_structure("main", HeatWeakStructureInputs(8.01, 20.0, -1.0, 4.0))
