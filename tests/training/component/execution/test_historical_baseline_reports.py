from __future__ import annotations

import json

import pytest

from tests.helpers.training.research.application.historical_extraction import _Evaluator, _Port
from tests.helpers.training.research.application.historical_replay import _ReplayEvaluator
from trader.training.application.extraction import HistoricalExtractor
from trader.training.application.replay import HistoricalBaselineReplayer
from trader.training.infra.research.baseline_reports import BaselineReportConflictError, JsonBaselineReportIndex


def test_r3_report_is_immutable_verifiable_and_idempotent(tmp_path) -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)
    evidence = JsonBaselineReportIndex(tmp_path)

    first = evidence.write(report)
    second = evidence.write(report)

    assert first == second
    assert evidence.verify().report_hash == report.report_hash
    payload = json.loads((tmp_path / "historical-baseline-report.json").read_text(encoding="utf-8"))
    assert "schema_version" not in payload
    assert payload["report_hash"] == report.report_hash
    assert payload["research_identity"] == report.research_identity
    assert payload["research_spec_hash"] == report.research_spec_hash
    assert evidence.verify() == report


def test_r3_report_rejects_tampering_and_identity_conflicts(tmp_path) -> None:
    extraction = HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(extraction)
    evidence = JsonBaselineReportIndex(tmp_path)
    evidence.write(report)

    path = tmp_path / "historical-baseline-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "replayed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineReportConflictError, match="hash"):
        evidence.verify()

    path.unlink()
    evidence.write(report)
    conflicting_extraction = HistoricalExtractor(_Port(), _Evaluator(61.0)).extract()
    conflicting = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(conflicting_extraction)
    with pytest.raises(BaselineReportConflictError):
        evidence.write(conflicting)


@pytest.mark.parametrize("changed", ("old_tag", "missing_identity"))
def test_report_rejects_incompatible_structure_even_with_a_valid_payload_hash(tmp_path, changed) -> None:
    from trader.training.domain.evaluation.research_identity import CanonicalResearchHash

    report = HistoricalBaselineReplayer(_ReplayEvaluator()).replay(HistoricalExtractor(_Port(), _Evaluator()).extract())
    index = JsonBaselineReportIndex(tmp_path)
    index.write(report)
    path = tmp_path / "historical-baseline-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("report_hash")
    if changed == "old_tag":
        payload["schema_version"] = "historical_baseline_report"
    else:
        payload.pop("research_identity")
    payload["report_hash"] = CanonicalResearchHash(payload)
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(BaselineReportConflictError):
        index.verify()

    assert path.read_bytes() == before
