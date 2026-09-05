from __future__ import annotations

import pytest

from trader.domain.recommendation.model_scoring import (
    V3_EXPOSURE_CONTRACT,
    ExposureContract,
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


def test_v3_residualization_removes_industry_before_board_amount_exposure() -> None:
    values = (1.0, 3.0, 2.0, 8.0, 4.0, 6.0)
    boards = ("main", "main", "main", "star", "star", "star")
    industries = ("bank", "bank", "software", "bank", "software", "software")
    amounts = (10.0, 20.0, 40.0, 15.0, 30.0, 60.0)

    result = residualize_exposure(
        values,
        boards,
        amounts,
        industries=industries,
        contract=V3_EXPOSURE_CONTRACT,
    )

    assert len(result) == len(values)
    assert sum(result) == pytest.approx(0.0)
    assert sum(
        value for value, industry in zip(result, industries, strict=True) if industry == "bank"
    ) == pytest.approx(0.0)
    assert sum(
        value for value, industry in zip(result, industries, strict=True) if industry == "software"
    ) == pytest.approx(0.0)


def test_v3_residualization_requires_complete_industry_exposure() -> None:
    with pytest.raises(ValueError, match="industries"):
        residualize_exposure(
            (1.0, 2.0),
            ("main", "main"),
            (10.0, 20.0),
            industries=("bank", ""),
            contract=V3_EXPOSURE_CONTRACT,
        )

    with pytest.raises(ValueError, match="contract order"):
        ExposureContract(("market", "industry", "log_average_amount_20d"))
