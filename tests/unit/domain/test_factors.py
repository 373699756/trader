from __future__ import annotations

import pytest

from trader.recommendation.domain.market.factors import (
    BandScore,
    PercentileScores,
    PercentileScoresWithMetadata,
    RoundScore,
    WeightedScore,
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
    assert BandScore(value, 0.0, 0.2, 1.8, 3.5) == pytest.approx(expected)


def test_band_score_treats_non_finite_as_missing_and_rejects_invalid_bounds() -> None:
    assert BandScore(float("nan"), 0.0, 0.2, 1.8, 3.5) == 50.0
    assert BandScore(float("inf"), 0.0, 0.2, 1.8, 3.5) == 50.0
    with pytest.raises(ValueError, match="band boundaries"):
        BandScore(1.0, 0.0, 0.0, 1.8, 3.5)


def test_percentile_scores_use_average_rank_for_ties() -> None:
    scores = PercentileScores({"a": 1.0, "b": 2.0, "c": 2.0, "d": 4.0, "missing": None})

    assert scores == {
        "a": 0.0,
        "b": 50.0,
        "c": 50.0,
        "d": 100.0,
        "missing": 50.0,
    }


def test_percentile_scores_winsorize_and_report_replay_metadata() -> None:
    scores, metadata = PercentileScoresWithMetadata(
        {"low": -1000.0, "a": 1.0, "b": 1.0, "high": 1000.0, "missing": float("nan")}
    )

    assert scores == {"low": 0.0, "a": 50.0, "b": 50.0, "high": 100.0, "missing": 50.0}
    assert metadata.sample_size == 4
    assert metadata.missing_count == 1
    assert metadata.lower_bound == pytest.approx(-924.925)
    assert metadata.upper_bound == pytest.approx(925.075)


def test_percentile_scores_single_sample_is_neutral_and_replayable() -> None:
    scores, metadata = PercentileScoresWithMetadata({"only": 7.0, "missing": None})

    assert scores == {"only": 50.0, "missing": 50.0}
    assert metadata.lower_bound == 7.0
    assert metadata.upper_bound == 7.0
    assert metadata.sample_size == 1
    assert metadata.missing_count == 1


def test_round_score_uses_round_half_up() -> None:
    assert RoundScore(83.405) == 83.41
    assert RoundScore(-2.0) == 0.0
    assert RoundScore(101.0) == 100.0


def test_weighted_score_rejects_component_drift() -> None:
    with pytest.raises(ValueError, match="component mismatch"):
        WeightedScore({"one": 50.0}, {"one": 0.5, "two": 0.5})
