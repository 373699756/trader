from __future__ import annotations

import pytest

from trader.domain.recommendation.model_scoring import (
    percentile_ranks,
    positive_utility_scores,
    residualize_exposure,
)


def test_percentile_and_positive_utility_scores_are_deterministic() -> None:
    values = (0.2, -0.1, 0.4)
    assert percentile_ranks(values) == (0.5, 0.0, 1.0)
    assert positive_utility_scores(values) == (50.0, 0.0, 100.0)


def test_residualization_removes_market_and_board_exposure() -> None:
    result = residualize_exposure((1.0, 2.0, 3.0, 4.0), ("main", "main", "star", "star"), (10.0, 10.0, 10.0, 10.0))
    assert result == pytest.approx((-0.5, 0.5, -0.5, 0.5))


def test_residualization_rejects_invalid_exposure_vectors() -> None:
    with pytest.raises(ValueError, match="same non-empty length"):
        residualize_exposure((1.0,), ("main", "star"), (10.0, 10.0))
    with pytest.raises(ValueError, match="amounts positive"):
        residualize_exposure((1.0,), ("main",), (0.0,))
