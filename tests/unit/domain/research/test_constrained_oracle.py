from __future__ import annotations

import math
from collections import Counter
from itertools import combinations
from random import Random

import pytest

from trader.training.domain.evaluation.constrained_oracle import (
    ConstrainedOracle,
    ConstrainedOracleCandidate,
    ConstrainedOraclePolicy,
)

POLICY = ConstrainedOraclePolicy(6, 3, 2)


def test_crossing_constraints_require_replacing_the_greedy_highest_return() -> None:
    candidates = (
        ConstrainedOracleCandidate("600001", "main", "X", 0.10),
        ConstrainedOracleCandidate("600002", "main", "X", 0.09),
        ConstrainedOracleCandidate("600003", "main", "Y", 0.08),
        ConstrainedOracleCandidate("600004", "main", "Z", 0.07),
        ConstrainedOracleCandidate("300001", "chinext", "X", 0.06),
        ConstrainedOracleCandidate("300002", "chinext", "X", 0.05),
    )

    result = ConstrainedOracle(candidates, POLICY)

    # Greedy takes 0.10 + 0.09 + 0.08, exhausts main/X, and falsely calls 0.27 optimal.
    assert result.net_return_sum == pytest.approx(0.31)
    assert result.selected_codes == ("300001", "600001", "600003", "600004")
    assert ConstrainedOracle(tuple(reversed(candidates)), POLICY) == result


def test_bounded_oracle_matches_exhaustive_subsets_with_cash_and_crossing_exposures() -> None:
    random = Random(77)
    for _case in range(30):
        candidates = tuple(
            ConstrainedOracleCandidate(
                f"{600000 + index:06d}",
                ("main", "chinext", "star")[random.randrange(3)],
                f"industry-{random.randrange(4)}",
                random.randrange(-3, 8) / 100,
            )
            for index in range(9)
        )
        legal_subsets = tuple(
            subset
            for count in range(7)
            for subset in combinations(candidates, count)
            if all(value <= 3 for value in Counter(item.board for item in subset).values())
            and all(value <= 2 for value in Counter(item.industry for item in subset).values())
        )
        optimum = max(math.fsum(item.net_return for item in subset) for subset in legal_subsets)
        result = ConstrainedOracle(candidates, POLICY)
        assert result.net_return_sum == pytest.approx(optimum, abs=1e-12)
        selected = tuple(item for item in candidates if item.code in result.selected_codes)
        assert selected in legal_subsets
        assert math.fsum(item.net_return for item in selected) == pytest.approx(optimum)


def test_oracle_keeps_nonpositive_slots_in_cash_and_breaks_positive_ties_by_code() -> None:
    rows = tuple(
        ConstrainedOracleCandidate(f"{600000 + index:06d}", "main", "X", value)
        for index, value in enumerate((0.01, 0.01, 0.01, 0.0, -0.01))
    )

    result = ConstrainedOracle(rows, POLICY)

    assert result.selected_codes == ("600000", "600001")
    assert result.net_return_sum == 0.02
    assert ConstrainedOracle(rows[3:], POLICY).selected_codes == ()
    assert ConstrainedOracle((), POLICY).net_return_sum == 0.0
    with pytest.raises(ValueError, match="unique"):
        ConstrainedOracle((rows[0], rows[0]), POLICY)


def test_production_constraints_have_no_industry_limit_and_match_exhaustive_optimum() -> None:
    candidates = tuple(
        ConstrainedOracleCandidate(f"{600000 + index:06d}", ("main", "star")[index // 5], "same-industry", value)
        for index, value in enumerate((0.10, 0.09, 0.08, 0.07, 0.06, 0.05, 0.04, -0.01, 0.0))
    )
    policy = ConstrainedOraclePolicy(6, 4, None)
    result = ConstrainedOracle(candidates, policy)
    legal = (
        subset
        for count in range(7)
        for subset in combinations(candidates, count)
        if all(value <= 4 for value in Counter(item.board for item in subset).values())
    )
    assert result.net_return_sum == pytest.approx(
        max(math.fsum(item.net_return for item in subset) for subset in legal)
    )
    assert len(result.selected_codes) == 6
    assert ConstrainedOracle(tuple(reversed(candidates)), policy) == result
