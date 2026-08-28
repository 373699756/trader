from __future__ import annotations

import pytest

from tests.unit.application.research.test_cost_aware_selection import _shadow_report
from trader.application.research.cost_aware_selection import ScoreTomorrowCostAwareSelection
from trader.infra.research.cost_aware_selection_artifacts import (
    CostAwareSelectionArtifactConflictError,
    CostAwareSelectionArtifactStore,
)


def test_cost_aware_selection_artifact_is_idempotent_and_tamper_evident(tmp_path) -> None:
    report = ScoreTomorrowCostAwareSelection().build(_shadow_report())
    store = CostAwareSelectionArtifactStore(tmp_path)

    assert store.seal(report) == report.content_hash
    assert store.seal(report) == report.content_hash
    artifact = tmp_path / report.selection_spec_hash / report.parent_report_hash / "selection-report.json"
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace('"top_k":6', '"top_k":5'),
        encoding="utf-8",
    )

    with pytest.raises(CostAwareSelectionArtifactConflictError, match="tampered"):
        store.seal(report)
