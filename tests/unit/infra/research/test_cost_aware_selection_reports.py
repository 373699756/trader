from __future__ import annotations

import pytest

from tests.unit.application.research.test_cost_aware_selection import _shadow_report
from trader.training.application.cost_aware_selection import TomorrowCostAwareSelectionEvaluator
from trader.training.infra.research.cost_aware_selection_reports import (
    CostAwareSelectionReportConflictError,
    CostAwareSelectionReportIndex,
)


def test_cost_aware_selection_artifact_is_idempotent_and_tamper_evident(tmp_path) -> None:
    report = TomorrowCostAwareSelectionEvaluator().build(_shadow_report())
    archive = CostAwareSelectionReportIndex(tmp_path)

    assert archive.seal(report) == report.content_hash
    assert archive.seal(report) == report.content_hash
    artifact = tmp_path / report.selection_spec_hash / report.parent_report_hash / "selection-report.json"
    artifact.write_text(
        artifact.read_text(encoding="utf-8").replace('"top_k":6', '"top_k":5'),
        encoding="utf-8",
    )

    with pytest.raises(CostAwareSelectionReportConflictError, match="tampered"):
        archive.seal(report)
