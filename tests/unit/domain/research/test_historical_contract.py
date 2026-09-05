from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from trader.domain.research.historical import (
    CostSettlementBasis,
    HistoricalCandidateSummary,
    ResearchDataLineage,
    ScoreComponent,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
OBSERVED_AT = datetime(2026, 8, 10, 14, 50, tzinfo=SHANGHAI)


def _lineage() -> ResearchDataLineage:
    return ResearchDataLineage(
        source="unified_data_plane",
        source_time=OBSERVED_AT,
        received_at=OBSERVED_AT,
        quality_status="complete",
        content_version="market-initial",
        content_hash="a" * 64,
    )


def _candidate() -> HistoricalCandidateSummary:
    return HistoricalCandidateSummary(
        code="600001",
        board="main",
        feature_as_of=OBSERVED_AT,
        lineage=_lineage(),
        candidate_components=(ScoreComponent("liquidity", 1.0, 80.0),),
        final_components=(ScoreComponent("trend", 0.7, 75.0), ScoreComponent("missing", 0.3, None)),
        production_candidate_score=80.0,
        production_top120=True,
    )


def test_candidate_summary_is_immutable_and_preserves_missing_components() -> None:
    candidate = _candidate()

    assert candidate.final_components[1].value is None
    with pytest.raises((AttributeError, TypeError)):
        candidate.code = "600002"  # type: ignore[misc]


def test_candidate_summary_rejects_invalid_weights_and_non_shanghai_times() -> None:
    with pytest.raises(ValueError, match="sum to one"):
        replace(_candidate(), candidate_components=(ScoreComponent("partial", 0.5, 80.0),))
    with pytest.raises(ValueError, match="Asia/Shanghai"):
        replace(_candidate(), feature_as_of=datetime(2026, 8, 10, 14, 50))
    with pytest.raises(ValueError, match="core missing gate"):
        replace(_candidate(), candidate_core_missing_ratio=0.31)


def test_lineage_rejects_future_receipt_order_and_invalid_hash() -> None:
    with pytest.raises(ValueError, match="before its source"):
        replace(_lineage(), received_at=OBSERVED_AT - timedelta(seconds=1))
    with pytest.raises(ValueError, match="identity"):
        replace(_lineage(), content_hash="not-a-hash")


def test_settlement_basis_requires_a_later_label_and_finite_cost_inputs() -> None:
    basis = CostSettlementBasis(
        code="600001",
        board="main",
        decision_date=date(2026, 8, 10),
        label_date=date(2026, 8, 11),
        gross_excess_return=0.025,
        mae_atr20=-0.4,
        turnover=0.3,
    )

    assert basis.gross_excess_return == 0.025
    with pytest.raises(ValueError, match="follow"):
        replace(basis, label_date=basis.decision_date)
