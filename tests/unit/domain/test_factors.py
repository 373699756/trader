from __future__ import annotations

import pytest

from trader.domain.factors import band_score, percentile_scores, round_score, weighted_score


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


def test_percentile_scores_use_average_rank_for_ties() -> None:
    scores = percentile_scores({"a": 1.0, "b": 2.0, "c": 2.0, "d": 4.0, "missing": None})

    assert scores == {
        "a": 0.0,
        "b": 50.0,
        "c": 50.0,
        "d": 100.0,
        "missing": 50.0,
    }


def test_round_score_uses_round_half_up() -> None:
    assert round_score(83.405) == 83.41
    assert round_score(-2.0) == 0.0
    assert round_score(101.0) == 100.0


def test_weighted_score_rejects_component_drift() -> None:
    with pytest.raises(ValueError, match="component mismatch"):
        weighted_score({"one": 50.0}, {"one": 0.5, "two": 0.5})
