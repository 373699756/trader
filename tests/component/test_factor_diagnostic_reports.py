from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.unit.application.research.test_factor_diagnostics import _evidence
from trader.application.research.factor_diagnostics import ScoreNativeFactorDiagnostics
from trader.infra.research.factor_diagnostic_reports import (
    FactorDiagnosticReportConflictError,
    JsonFactorDiagnosticReportStore,
)


def test_factor_report_is_immutable_verifiable_and_idempotent(tmp_path) -> None:
    extraction, baseline, dimensions = _evidence()
    report = ScoreNativeFactorDiagnostics().evaluate(extraction, baseline, dimensions)
    store = JsonFactorDiagnosticReportStore(tmp_path)

    assert store.write(report) == report
    assert store.write(report) == report
    assert store.verify() == report
    payload = json.loads((tmp_path / "score-factor-diagnostic-report.json").read_text(encoding="utf-8"))
    assert payload["schema_version"] == "score_factor_diagnostic_report"
    assert payload["production_authority"] is False
    assert payload["report_hash"] == report.report_hash


def test_factor_report_rejects_tampering_and_same_identity_conflicts(tmp_path) -> None:
    extraction, baseline, dimensions = _evidence()
    report = ScoreNativeFactorDiagnostics().evaluate(extraction, baseline, dimensions)
    store = JsonFactorDiagnosticReportStore(tmp_path)
    store.write(report)

    path = tmp_path / "score-factor-diagnostic-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["production_authority"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(FactorDiagnosticReportConflictError, match="hash or schema"):
        store.verify()

    path.unlink()
    store.write(report)
    changed_record = replace(dimensions.records[0], market_cap=9999.0)
    changed_dimensions = replace(dimensions, records=(changed_record, *dimensions.records[1:]))
    conflicting = ScoreNativeFactorDiagnostics().evaluate(extraction, baseline, changed_dimensions)
    with pytest.raises(FactorDiagnosticReportConflictError, match="identity conflict"):
        store.write(conflicting)
