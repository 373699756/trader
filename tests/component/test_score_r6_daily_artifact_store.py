from __future__ import annotations

import json

import pytest

from trader.application.research.historical_screening import HistoricalArchiveManifest, HistoricalArchiveStatus
from trader.application.research.score_r6_daily_models import ScoreR6DailyReport
from trader.application.research.score_r6_models import ScoreR6Metrics
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.score_r6_daily import SCORE_R6_DAILY_SPEC, iter_score_r6_daily_candidates
from trader.infra.research.score_r6_daily_artifacts import (
    ScoreR6DailyArtifactConflictError,
    ScoreR6DailyArtifactStore,
)


def _report() -> ScoreR6DailyReport:
    archive = HistoricalArchiveStatus(
        initialized=True,
        research_identity=SCORE_H0_V1_SPEC.research_identity,
        universe_count=100,
        completed_codes=98,
        spec_hash=SCORE_H0_V1_SPEC.content_hash,
    )
    manifest = HistoricalArchiveManifest(
        SCORE_H0_V1_SPEC.research_identity,
        SCORE_H0_V1_SPEC.content_hash,
        "1" * 64,
        "2" * 64,
        (),
    )
    empty = ScoreR6Metrics(0, 0, 0, None, None, None, None, None, None, None, None)
    return ScoreR6DailyReport(
        status="historical_rejected",
        research_identity=SCORE_R6_DAILY_SPEC.research_identity,
        research_spec_hash=SCORE_R6_DAILY_SPEC.content_hash,
        archive=archive,
        archive_manifest=manifest,
        selected_candidate=iter_score_r6_daily_candidates(SCORE_R6_DAILY_SPEC)[0],
        training=empty,
        validation=empty,
        baseline_training=empty,
        baseline_validation=empty,
        historical_gate_passed=False,
        failure_reasons=("daily_trend_no_training_candidate",),
        limitations=("current_universe_survivorship_bias",),
        promotion_authority=False,
        schema_version=SCORE_R6_DAILY_SPEC.report_schema_version,
    )


def test_daily_trend_report_is_idempotent_tamper_evident_and_inspectable(tmp_path) -> None:  # noqa: ANN001
    store = ScoreR6DailyArtifactStore(tmp_path)
    report = _report()

    assert store.seal(report) == report.content_hash
    assert store.seal(report) == report.content_hash
    assert store.inspect() == {
        "report_hash": report.content_hash,
        "status": "historical_rejected",
        "historical_gate_passed": False,
        "selected_candidate_hash": report.selected_candidate.content_hash,
        "failure_reasons": ["daily_trend_no_training_candidate"],
        "promotion_authority": False,
    }

    path = tmp_path / SCORE_R6_DAILY_SPEC.research_identity / "historical-report.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["historical_gate_passed"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ScoreR6DailyArtifactConflictError, match="hash"):
        store.inspect()


def test_daily_trend_inspection_is_read_only_before_a_report_exists(tmp_path) -> None:  # noqa: ANN001
    store = ScoreR6DailyArtifactStore(tmp_path)

    assert store.inspect()["status"] == "not_run"
    assert not (tmp_path / SCORE_R6_DAILY_SPEC.research_identity).exists()
