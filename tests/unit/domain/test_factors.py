from __future__ import annotations

import pytest

from trader.domain.market.factors import (
    band_score,
    percentile_scores,
    percentile_scores_with_metadata,
    round_score,
    weighted_score,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, 0.0),
        (0.1, 50.0),
        (0.2, 100.0),
        (1.8, 100.0),
        (2.65, 50.0),
        (3.5, 0.0),
        (None, 50.0),
    ],
)
def test_band_score_contract(value, expected) -> None:
    assert band_score(value, 0.0, 0.2, 1.8, 3.5) == pytest.approx(expected)


def test_band_score_treats_non_finite_as_missing_and_rejects_invalid_bounds() -> None:
    assert band_score(float("nan"), 0.0, 0.2, 1.8, 3.5) == 50.0
    assert band_score(float("inf"), 0.0, 0.2, 1.8, 3.5) == 50.0
    with pytest.raises(ValueError, match="band boundaries"):
        band_score(1.0, 0.0, 0.0, 1.8, 3.5)


def test_percentile_scores_use_average_rank_for_ties() -> None:
    scores = percentile_scores({"a": 1.0, "b": 2.0, "c": 2.0, "d": 4.0, "missing": None})

    assert scores == {
        "a": 0.0,
        "b": 50.0,
        "c": 50.0,
        "d": 100.0,
        "missing": 50.0,
    }


def test_percentile_scores_winsorize_and_report_replay_metadata() -> None:
    scores, metadata = percentile_scores_with_metadata(
        {"low": -1000.0, "a": 1.0, "b": 1.0, "high": 1000.0, "missing": float("nan")}
    )

    assert scores == {"low": 0.0, "a": 50.0, "b": 50.0, "high": 100.0, "missing": 50.0}
    assert metadata.sample_size == 4
    assert metadata.missing_count == 1
    assert metadata.lower_bound == pytest.approx(-924.925)
    assert metadata.upper_bound == pytest.approx(925.075)


def test_percentile_scores_single_sample_is_neutral_and_replayable() -> None:
    scores, metadata = percentile_scores_with_metadata({"only": 7.0, "missing": None})

    assert scores == {"only": 50.0, "missing": 50.0}
    assert metadata.lower_bound == 7.0
    assert metadata.upper_bound == 7.0
    assert metadata.sample_size == 1
    assert metadata.missing_count == 1


def test_round_score_uses_round_half_up() -> None:
    assert round_score(83.405) == 83.41
    assert round_score(-2.0) == 0.0
    assert round_score(101.0) == 100.0


def test_weighted_score_rejects_component_drift() -> None:
    with pytest.raises(ValueError, match="component mismatch"):
        weighted_score({"one": 50.0}, {"one": 0.5, "two": 0.5})
