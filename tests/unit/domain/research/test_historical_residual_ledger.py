from dataclasses import replace
from datetime import date

import pytest

from trader.domain.research.historical_residual_ledger import (
    HistoricalOutcomeRecord,
    HistoricalPredictionRecord,
    ResidualJoinKey,
    join_prediction_outcome,
    summarize_residuals,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _prediction(*, selected: bool = False) -> HistoricalPredictionRecord:
    return HistoricalPredictionRecord(
        key=ResidualJoinKey("tomorrow", date(2024, 1, 2), "14:50", "600001", 1),
        parent_split_hash=_HASH_A,
        feature_hash=_HASH_A,
        model_hash=_HASH_B,
        board="main",
        industry="industrial",
        market_state="normal",
        liquidity_state="liquid",
        volatility_state="normal",
        predicted_net_excess_return=0.01,
        score=80.0,
        filter_state="passed",
        selected=selected,
        selection_reason="top6" if selected else "below_top6",
    )


def _outcome(*, actual: float = 0.015) -> HistoricalOutcomeRecord:
    return HistoricalOutcomeRecord(
        key=_prediction().key,
        parent_split_hash=_HASH_A,
        gross_return=actual + 0.003,
        benchmark_return=0.001,
        round_trip_cost=0.002,
        actual_net_excess_return=actual,
        mae_atr20=-0.4,
        severe_loss=False,
        label_status="matured",
    )


def test_prediction_and_outcome_join_only_on_exact_typed_identity_and_parent_hash() -> None:
    joined = join_prediction_outcome(_prediction(), _outcome())

    assert joined.prediction_error == pytest.approx(0.005)
    assert joined.production_authority is False
    with pytest.raises(ValueError, match="parent split"):
        join_prediction_outcome(_prediction(), replace(_outcome(), parent_split_hash=_HASH_B))
    with pytest.raises(ValueError, match="identity"):
        join_prediction_outcome(
            _prediction(),
            replace(_outcome(), key=replace(_outcome().key, code="600002")),
        )


def test_pending_labels_remain_null_and_cannot_be_joined_or_faked_as_zero() -> None:
    pending = HistoricalOutcomeRecord(
        key=_prediction().key,
        parent_split_hash=_HASH_A,
        gross_return=None,
        benchmark_return=None,
        round_trip_cost=None,
        actual_net_excess_return=None,
        mae_atr20=None,
        severe_loss=None,
        label_status="label_pending",
    )

    with pytest.raises(ValueError, match="not matured"):
        join_prediction_outcome(_prediction(), pending)
    with pytest.raises(ValueError, match="pending"):
        replace(pending, actual_net_excess_return=0.0)
    with pytest.raises(ValueError, match="severe-loss"):
        replace(_outcome(), mae_atr20=-1.5, severe_loss=False)
    d25_key = ResidualJoinKey("d25", date(2024, 1, 2), "14:50", "600001", 2)
    d25 = replace(_outcome(), key=d25_key, mae_atr20=-2.0, severe_loss=False)
    assert d25.severe_loss is False


def test_residual_summary_uses_trade_dates_and_separates_selected_population() -> None:
    selected = join_prediction_outcome(_prediction(selected=True), _outcome(actual=0.015))
    unselected_prediction = replace(
        _prediction(),
        key=replace(_prediction().key, code="600002"),
        feature_hash=_HASH_B,
    )
    unselected_outcome = replace(_outcome(actual=-0.005), key=unselected_prediction.key)

    summary = summarize_residuals((selected, join_prediction_outcome(unselected_prediction, unselected_outcome)))

    assert summary.evaluated_trade_dates == 1
    assert summary.evaluated_rows == 2
    assert summary.selected_rows == 1
    assert summary.unselected_rows == 1
    assert summary.mean_error == pytest.approx(-0.005)
    assert summary.mean_absolute_error == pytest.approx(0.01)
    assert summary.direction_hit_rate == pytest.approx(0.5)
    assert summary.terminal_holdout_opened is False
    assert summary.production_authority is False
