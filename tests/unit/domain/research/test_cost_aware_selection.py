from __future__ import annotations

import pytest

from trader.domain.research.cost_aware_selection import (
    CostAwareCandidate,
    CostAwareSelectionPolicy,
    select_cost_aware,
)


def test_net_utility_ranking_uses_stock_cost_and_fixed_constraints() -> None:
    policy = CostAwareSelectionPolicy(horizon="d25")
    candidates = (
        _candidate("600001", "main", "bank", 0.020, 0.015),
        _candidate("600002", "main", "bank", 0.019, 0.005),
        _candidate("600003", "main", "energy", 0.018, 0.004),
        _candidate("600004", "main", "utility", 0.017, 0.004),
        _candidate("300001", "chinext", "software", 0.016, 0.001),
        _candidate("300002", "chinext", "hardware", 0.015, 0.001),
        _candidate("300003", "chinext", "health", 0.014, 0.001),
    )

    result = select_cost_aware(candidates, policy)

    assert result.selected_codes[0] == "300001"
    assert len(result.selected_codes) <= 6
    selected = tuple(item for item in result.evaluations if item.selected_rank is not None)
    assert (
        max(
            sum(item.board == board for item in selected) / len(selected) for board in {item.board for item in selected}
        )
        <= 0.60
    )
    assert (
        max(sum(item.industry == industry for item in selected) for industry in {item.industry for item in selected})
        <= 2
    )
    assert next(item for item in result.evaluations if item.code == "600001").net_utility == pytest.approx(0.005)


def test_d25_entry_threshold_is_higher_than_maintenance_threshold() -> None:
    policy = CostAwareSelectionPolicy(horizon="d25")
    result = select_cost_aware(
        (
            _candidate("600001", "main", "bank", 0.003, 0.002, incumbent=True),
            _candidate("300001", "chinext", "software", 0.003, 0.002, incumbent=True),
            _candidate("600002", "main", "energy", 0.003, 0.002),
        ),
        policy,
    )

    assert policy.entry_threshold > policy.maintenance_threshold
    assert result.selected_codes == ("300001", "600001")
    assert next(item for item in result.evaluations if item.code == "600002").skip_reason == "entry_threshold"


def test_tomorrow_rejects_cross_period_incumbency_and_allows_empty_pool() -> None:
    policy = CostAwareSelectionPolicy(horizon="tomorrow")

    with pytest.raises(ValueError, match="Tomorrow"):
        select_cost_aware((_candidate("600001", "main", "bank", 0.01, 0.001, incumbent=True),), policy)

    result = select_cost_aware(
        (
            _candidate("600001", "main", "bank", 0.001, 0.002),
            _candidate("300001", "chinext", "software", 0.001, 0.002),
        ),
        policy,
    )

    assert result.selected_codes == ()
    assert all(item.skip_reason == "entry_threshold" for item in result.evaluations)


def test_industry_whitespace_cannot_bypass_concentration_limit() -> None:
    result = select_cost_aware(
        (
            _candidate("600001", "main", "bank", 0.010, 0.001, incumbent=True),
            _candidate("300001", "chinext", " bank ", 0.009, 0.001, incumbent=True),
            _candidate("688001", "star", "bank", 0.008, 0.001, incumbent=True),
            _candidate("600002", "main", "energy", 0.007, 0.001, incumbent=True),
            _candidate("300002", "chinext", "software", 0.006, 0.001, incumbent=True),
        ),
        CostAwareSelectionPolicy(horizon="d25"),
    )

    selected = tuple(item for item in result.evaluations if item.selected_rank is not None)
    assert sum(item.industry == "bank" for item in selected) == 2
    assert next(item for item in result.evaluations if item.code == "300001").industry == "bank"


def _candidate(
    code: str,
    board: str,
    industry: str,
    gross_expected_excess: float,
    estimated_cost: float,
    *,
    incumbent: bool = False,
) -> CostAwareCandidate:
    return CostAwareCandidate(
        code=code,
        board=board,
        industry=industry,
        gross_expected_excess=gross_expected_excess,
        estimated_cost=estimated_cost,
        severe_loss_probability=0.1,
        uncertainty=0.0,
        incumbent=incumbent,
    )
