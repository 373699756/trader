from __future__ import annotations

import json

import pytest

from tests.unit.application.research.test_score_r2_extraction import _Evaluator, _Port
from tests.unit.application.research.test_score_r3_replay import _ReplayEvaluator
from trader.application.research.extraction import ScoreR2HistoricalExtractor
from trader.application.research.replay import ScoreR3BaselineReplayer
from trader.infra.research.baseline_reports import BaselineReportConflictError, JsonBaselineReportStore


def test_r3_report_is_immutable_verifiable_and_idempotent(tmp_path) -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(extraction)
    store = JsonBaselineReportStore(tmp_path)

    first = store.write(report)
    second = store.write(report)

    assert first == second
    assert store.verify().report_hash == report.report_hash
    payload = json.loads((tmp_path / "score-r3-baseline-report.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "score_r3_baseline_report_v1"
    assert payload["report_hash"] == report.report_hash


def test_r3_report_rejects_tampering_and_identity_conflicts(tmp_path) -> None:
    extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator()).extract()
    report = ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(extraction)
    store = JsonBaselineReportStore(tmp_path)
    store.write(report)

    path = tmp_path / "score-r3-baseline-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["status"] = "replayed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BaselineReportConflictError, match="hash"):
        store.verify()

    path.unlink()
    store.write(report)
    conflicting_extraction = ScoreR2HistoricalExtractor(_Port(), _Evaluator(61.0)).extract()
    conflicting = ScoreR3BaselineReplayer(_ReplayEvaluator()).replay(conflicting_extraction)
    with pytest.raises(BaselineReportConflictError):
        store.write(conflicting)
