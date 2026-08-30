from __future__ import annotations

from datetime import timedelta

import pytest

from trader.application.research.historical_screening import HistoricalArchiveManifest
from trader.application.research.tomorrow_historical_p2_screening import TomorrowHistoricalP2Row
from trader.domain.research.historical_screening import SCORE_H0_V1_SPEC
from trader.infra.research.tomorrow_manual_v1_model import (
    V1_FEATURE_IDS,
    fit_manual_v1_model,
    sealed_production_artifact_payload,
)


def _rows() -> tuple[TomorrowHistoricalP2Row, ...]:
    return tuple(
        TomorrowHistoricalP2Row(
            trade_date=SCORE_H0_V1_SPEC.training_start + timedelta(days=index),
            code=f"600{index:03d}",
            board="main",
            alpha_features=(
                0.0,
                0.0,
                0.0,
                index / 100.0,
                (index % 3) / 100.0,
                ((index * index) % 7) / 100.0,
            ),
            realized_volatility_20d=0.02,
            downside_semivariance_20d=0.01,
            drawdown_recovery_60d=0.9,
            amihud_20d=0.001,
            average_amount_20d=100_000_000.0,
            baseline_score=50.0,
            gross_excess_return=0.001 + index / 10_000.0,
            mae_atr20=-0.2,
        )
        for index in range(12)
    )


def test_manual_v1_fit_is_deterministic_bounded_and_provenance_bound() -> None:
    manifest = HistoricalArchiveManifest(
        SCORE_H0_V1_SPEC.research_identity,
        SCORE_H0_V1_SPEC.content_hash,
        "a" * 64,
        "b" * 64,
        (),
    )

    first = fit_manual_v1_model(iter(_rows()), SCORE_H0_V1_SPEC, manifest)
    second = fit_manual_v1_model(iter(_rows()), SCORE_H0_V1_SPEC, manifest)
    payload = sealed_production_artifact_payload(first)

    assert first == second
    assert first.feature_ids == V1_FEATURE_IDS
    assert first.training_rows == 12
    assert first.source_manifest_hash == manifest.content_hash
    assert payload["content_hash"] == first.content_hash
    assert payload["feature_contract"] == "h0_board_amount_residual_momentum_proxy_v1"


def test_manual_v1_fit_rejects_a_manifest_from_another_archive() -> None:
    manifest = HistoricalArchiveManifest("other_research", "", "a" * 64, "b" * 64, ())

    with pytest.raises(ValueError, match="exact H0 manifest"):
        fit_manual_v1_model(iter(_rows()), SCORE_H0_V1_SPEC, manifest)
