from __future__ import annotations

import pytest

from trader.domain.research.baseline import (
    mean_rank_ic,
    population_spearman,
    quantile_bucket,
    stock_net_contribution,
)


def test_stock_net_contribution_reuses_the_settlement_basis_cost_units() -> None:
    assert stock_net_contribution(0.5, 0.04, 0.25, 0.002) == pytest.approx(0.01975)
    assert stock_net_contribution(0.0, 0.04, 0.25, 0.002) == 0.0


def test_population_spearman_uses_average_ties_and_requires_five_pairs() -> None:
    assert population_spearman(((10.0, 1.0), (20.0, 2.0), (20.0, 3.0), (40.0, 4.0), (50.0, 5.0))) == pytest.approx(
        0.9746794344808963
    )
    assert population_spearman(((10.0, 1.0),) * 4) is None
    assert mean_rank_ic((None, 0.2, -0.1)) == pytest.approx(0.05)


def test_quantile_bucket_is_deterministic_for_ties_and_remainders() -> None:
    rows = (("600003", 80.0), ("600001", 80.0), ("600002", 70.0), ("600005", 60.0), ("600004", 50.0), ("600006", 40.0))

    assert quantile_bucket(rows) == {
        "600001": 5,
        "600003": 5,
        "600002": 4,
        "600005": 3,
        "600004": 2,
        "600006": 1,
    }
    assert quantile_bucket(tuple((f"60000{index}", 50.0) for index in range(1, 6))) == {
        "600001": 5,
        "600002": 4,
        "600003": 3,
        "600004": 2,
        "600005": 1,
    }
