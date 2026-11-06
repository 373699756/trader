from __future__ import annotations

import pytest

from trader.training.domain.evaluation.factor_diagnostics import (
    FactorConcentration,
    InformationCoefficientRatio,
    Monotonicity,
    PopulationPearson,
    TopBucketTurnover,
)


def test_factor_ic_and_icir_use_frozen_minimums_and_sample_deviation() -> None:
    increasing = tuple((float(value), float(value * 2)) for value in range(1, 6))

    assert PopulationPearson(increasing) == pytest.approx(1.0)
    assert PopulationPearson(increasing[:4]) is None
    assert InformationCoefficientRatio((0.1, 0.2, 0.3)) == pytest.approx(2.0)
    assert InformationCoefficientRatio((None, 0.2)) is None
    assert InformationCoefficientRatio((0.2, 0.2)) is None


def test_monotonicity_and_turnover_keep_unknowns_explicit() -> None:
    assert Monotonicity((0.01, 0.02, 0.02, 0.04, 0.03)) == pytest.approx((0.75, 0.02))
    assert Monotonicity((None, 0.02, 0.03, 0.04, 0.05)) == (None, None)
    assert TopBucketTurnover(("600001", "600002"), ("600002", "600003", "600004")) == pytest.approx(2 / 3)
    assert TopBucketTurnover((), ("600001",)) is None


def test_factor_concentration_aggregates_positive_q5_contribution_by_stock() -> None:
    assert FactorConcentration(
        (("600001", 0.03), ("600001", 0.02), ("600002", 0.03), ("600003", -0.50))
    ) == pytest.approx((0.625, 1.0))
    assert FactorConcentration((("600001", 0.03), ("600001", -0.04), ("600002", 0.02))) == (1.0, 1.0)
    assert FactorConcentration((("600001", 0.0), ("600002", -0.1))) == (None, None)
