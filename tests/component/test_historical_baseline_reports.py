from __future__ import annotations

import json

import pytest

from tests.unit.application.research.test_historical_extraction import _Evaluator, _Port
from tests.unit.application.research.test_historical_replay import _ReplayEvaluator
from trader.application.research.extraction import HistoricalExtractor
from trader.application.research.replay import HistoricalBaselineReplayer
from trader.infra.research.baseline_reports import BaselineReportConflictError, JsonBaselineReportArchive


def test_r3_report_is_immutable_verifiable_and_idempotent(tmp_path) -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)
    archive = JsonBaselineReportArchive(tmp_path)

    first = archive.write(report)
    second = archive.write(report)

    assert first == second
    assert archive.verify().report_hash == report.report_hash
    payload = json.loads((tmp_path / "historical-baseline-report.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "historical_baseline_report"
    assert payload["report_hash"] == report.report_hash
    assert "research_identity" not in payload
    assert "research_spec_hash" not in payload
    assert archive.verify() == report


def test_r3_report_rejects_tampering_and_identity_conflicts(tmp_path) -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)
    archive = JsonBaselineReportArchive(tmp_path)
    archive.write(report)

    path = tmp_path / "historical-baseline-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "replayed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineReportConflictError, match="hash"):
        archive.verify()

    path.unlink()
    archive.write(report)
    conflicting_extraction = HistoricalExtractor(_Port(), _Evaluator(61.0)).extract()
    conflicting = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(conflicting_extraction)
    with pytest.raises(BaselineReportConflictError):
        archive.write(conflicting)
