from __future__ import annotations

import pytest

from trader.training.domain.evaluation.historical import (
    CoverageShrunkScore,
    OptimisticComponentUpperBound,
    OptimisticFinalUpperBound,
    ScoreComponent,
)


def test_coverage_shrink_preserves_missing_and_shrinks_to_neutral() -> None:
    components = (ScoreComponent("known", 0.7, 80.0), ScoreComponent("missing", 0.3, None))

    assert CoverageShrunkScore(components) == 71.0
    assert components[1].value is None
    assert CoverageShrunkScore((ScoreComponent("missing", 1.0, None),)) == 50.0


def test_optimistic_upper_bound_keeps_confirmed_risk_and_reuses_only_recorded_facts() -> None:
    components = (ScoreComponent("known", 0.7, 80.0), ScoreComponent("missing", 0.3, None))

    assert OptimisticFinalUpperBound(components, mandatory_local_risk_penalty=5.0) == 81.0
    assert OptimisticComponentUpperBound(components) == 86.0
    assert OptimisticFinalUpperBound(
        components,
        mandatory_local_risk_penalty=5.0,
        recorded_deepseek_score=90.0,
        recorded_deepseek_risk_penalty=4.0,
    ) == pytest.approx(79.88)
    with pytest.raises(ValueError, match="requires a recorded score"):
        OptimisticFinalUpperBound(
            components,
            mandatory_local_risk_penalty=5.0,
            recorded_deepseek_risk_penalty=4.0,
        )
