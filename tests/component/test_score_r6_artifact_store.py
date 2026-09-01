from __future__ import annotations

import json

import pytest

from trader.application.research.historical_screening import (
    HistoricalArchiveManifest,
    HistoricalArchiveStatus,
)
from trader.application.research.score_r6 import ScoreR6HistoricalScreeningService
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6 import SCORE_R6_HISTORICAL_SPEC
from trader.infra.research.score_r6_artifacts import ScoreR6ArtifactConflictError, ScoreR6ArtifactStore


class _InsufficientEvidence:
    def inspect(self, research_identity: str) -> HistoricalArchiveStatus:
        return HistoricalArchiveStatus(
            initialized=True,
            research_identity=research_identity,
            universe_count=100,
            completed_codes=0,
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
        )

    def manifest(self, _spec) -> HistoricalArchiveManifest:  # noqa: ANN001
        return HistoricalArchiveManifest(
            research_identity=SCORE_H0_V1_SPEC.research_identity,
            spec_hash=SCORE_H0_V1_SPEC.content_hash,
            universe_hash="1" * 64,
            histories_hash="2" * 64,
            histories=(),
        )

    def score_r6_rows(self, _spec):  # noqa: ANN001, ANN201
        raise AssertionError("insufficient coverage must stop before loading rows")


def _report():  # noqa: ANN202
    return ScoreR6HistoricalScreeningService(_InsufficientEvidence()).execute(SCORE_R6_HISTORICAL_SPEC)


def test_historical_only_r6_report_is_idempotent_and_tamper_evident(tmp_path) -> None:  # noqa: ANN001
    store = ScoreR6ArtifactStore(tmp_path)
    report = _report()

    assert store.seal_historical(report) == report.content_hash
    assert store.seal_historical(report) == report.content_hash
    assert store.inspect() == {
        "historical_report_hash": report.content_hash,
        "historical_gate_passed": False,
        "validation_mode": "historical_only",
        "production_authority": False,
    }

    path = tmp_path / SCORE_R6_HISTORICAL_SPEC.research_identity / "historical-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["validation_mode"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScoreR6ArtifactConflictError, match="invalid"):
        store.inspect()


def test_legacy_r6_v1_report_is_not_reinterpreted_as_historical_only(tmp_path) -> None:  # noqa: ANN001
    legacy = tmp_path / "score_r6_historical_v1" / "historical-report.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('{"historical_gate_passed": true, "content_hash": "legacy"}', encoding="utf-8")

    assert ScoreR6ArtifactStore(tmp_path).inspect() == {
        "historical_report_hash": "",
        "historical_gate_passed": False,
        "validation_mode": "historical_only",
        "production_authority": False,
    }
