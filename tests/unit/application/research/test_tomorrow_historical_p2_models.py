from __future__ import annotations

from dataclasses import replace

import pytest

from trader.application.research.tomorrow_historical_p2_models import (
    TomorrowHistoricalP2GateMetrics,
    TomorrowHistoricalP2Report,
)
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.domain.research.tomorrow_historical_p2 import (
    TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
    TOMORROW_HISTORICAL_P2_SPEC,
)


def _passing_metrics() -> TomorrowHistoricalP2GateMetrics:
    return TomorrowHistoricalP2GateMetrics(
        archive_coverage=0.97,
        training_trade_dates=300,
        validation_trade_dates=120,
        validation_pairs=360,
        mean_net_increment_20bp=0.001,
        mean_net_increment_50bp=0.0008,
        mean_net_increment_100bp=0.0003,
        bootstrap_lower_bound_20bp=0.0001,
        baseline_severe_loss_rate=0.04,
        candidate_severe_loss_rate=0.04,
        turnover_increase=0.01,
        mean_rank_ic=0.02,
        top_bottom_quintile_spread=0.001,
        maximum_stock_positive_fraction=0.08,
        top_five_positive_fraction=0.25,
        maximum_board_fraction=0.60,
    )


def _passing_report() -> TomorrowHistoricalP2Report:
    return TomorrowHistoricalP2Report(
        research_spec_hash=TOMORROW_HISTORICAL_P2_SPEC.content_hash,
        source_spec_hash=SCORE_H0_V1_SPEC.content_hash,
        source_manifest_hash="a" * 64,
        source_universe_hash="e" * 64,
        source_histories_hash="f" * 64,
        candidate_id=TOMORROW_HISTORICAL_P2_CANDIDATE_ID,
        status="historical_passed",
        metrics=_passing_metrics(),
        training_evidence_hash="b" * 64,
        validation_evidence_hash="c" * 64,
        model_artifact_hash="d" * 64,
        failure_reasons=(),
    )


def test_p2_historical_report_can_only_authorize_later_forward_preregistration() -> None:
    report = _passing_report()

    assert report.status == "historical_passed"
    assert report.forward_preregistration_eligible is True
    assert report.production_authority is False
    assert report.schema_version == "score_tomorrow_historical_p2_report"
    assert report.source_universe_hash == "e" * 64
    assert report.source_histories_hash == "f" * 64
    assert len(report.content_hash) == 64


def test_p2_historical_report_rejects_false_pass_or_production_authority() -> None:
    report = _passing_report()

    with pytest.raises(ValueError, match="historical gates"):
        replace(report, metrics=replace(report.metrics, validation_pairs=299))
    with pytest.raises(ValueError, match="historical gates"):
        replace(report, metrics=replace(report.metrics, bootstrap_lower_bound_20bp=0.0))
    with pytest.raises(ValueError, match="historical gates"):
        replace(report, metrics=replace(report.metrics, mean_net_increment_100bp=None))
    with pytest.raises(ValueError, match="cannot authorize production"):
        replace(report, production_authority=True)
    with pytest.raises(ValueError, match="source identity"):
        replace(report, source_histories_hash="not-a-sha256")


def test_p2_rejected_report_requires_bounded_reasons_and_cannot_select_an_alternative() -> None:
    report = replace(
        _passing_report(),
        status="historical_rejected",
        metrics=replace(_passing_metrics(), mean_net_increment_20bp=-0.001),
        training_evidence_hash=None,
        validation_evidence_hash=None,
        model_artifact_hash=None,
        failure_reasons=("mean_increment_not_positive",),
    )

    assert report.forward_preregistration_eligible is False
    with pytest.raises(ValueError, match="fixed candidate"):
        replace(report, candidate_id="alternative_after_validation")
    with pytest.raises(ValueError, match="requires bounded failure reasons"):
        replace(report, failure_reasons=())
