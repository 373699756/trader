from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from trader.domain.research.terminal_holdout import (
    TerminalHoldoutRow,
    TerminalStatus,
    evaluate_terminal_holdout,
)


def _rows(count: int = 200, *, positive: bool = True) -> tuple[TerminalHoldoutRow, ...]:
    rows: list[TerminalHoldoutRow] = []
    for index in range(count):
        day = date(2025, 1, 1) + timedelta(days=index)
        for stock in range(20):
            actual = (0.008 + stock / 10000) if positive else -0.01
            rows.append(
                TerminalHoldoutRow(
                    trade_date=day,
                    code=f"60{stock:04d}",
                    board=("main", "chinext", "star")[stock % 3],
                    industry=f"industry-{stock % 10}",
                    market_state=("up", "down", "sideways")[index % 3],
                    volatility_state=("low", "high")[index % 2],
                    liquidity_state=("high", "low")[index % 2],
                    predicted_net_excess_return=actual,
                    actual_net_excess_returns=(actual, actual - 0.003, actual - 0.008),
                    baseline_net_excess_returns=(0.0, -0.003, -0.008),
                    selected=True,
                    baseline_selected=True,
                    severe_loss=False,
                    baseline_severe_loss=False,
                    mae_atr20=-0.3,
                    baseline_mae_atr20=-0.3,
                    point_in_time_parity=True,
                ),
            )
    return tuple(rows)


def test_terminal_holdout_validates_positive_candidate_against_local_baseline() -> None:
    report = evaluate_terminal_holdout(
        strategy="today",
        research_identity="score_today_historical_candidate_v1",
        parent_hash="a" * 64,
        candidate_hash="b" * 64,
        rows=_rows(),
    )

    assert report.status == "historical_validated"
    assert report.metrics.evaluated_trade_dates == 200
    assert report.metrics.mean_net_excess_returns[0] > 0
    assert report.production_authority is False
    assert len(report.content_hash) == 64


def test_terminal_holdout_does_not_open_when_parent_is_rejected() -> None:
    report = evaluate_terminal_holdout(
        strategy="today",
        research_identity="score_today_historical_candidate_v1",
        parent_hash="a" * 64,
        candidate_hash="b" * 64,
        rows=_rows(),
        parent_status="historical_rejected",
        parent_failure_reasons=("candidate_rejected",),
    )

    assert report.status == "historical_rejected"
    assert report.terminal_holdout_opened is False
    assert report.failure_reasons == ("candidate_rejected",)


def test_terminal_holdout_requires_point_in_time_parity() -> None:
    rows = list(_rows())
    rows[0] = TerminalHoldoutRow(**{**rows[0].__dict__, "point_in_time_parity": False})
    with pytest.raises(ValueError, match="point-in-time"):
        evaluate_terminal_holdout(
            strategy="today",
            research_identity="score_today_historical_candidate_v1",
            parent_hash="a" * 64,
            candidate_hash="b" * 64,
            rows=tuple(rows),
        )


def test_terminal_holdout_reports_data_insufficient_when_a_preregistered_state_is_missing() -> None:
    rows = tuple(replace(row, market_state="up") for row in _rows())
    report = evaluate_terminal_holdout(
        strategy="today",
        research_identity="score_today_historical_candidate_v1",
        parent_hash="a" * 64,
        candidate_hash="b" * 64,
        rows=rows,
    )

    assert report.status == "historical_data_insufficient"
    assert "state_sample_below_40" in report.failure_reasons


def test_terminal_holdout_rejects_a_second_terminal_open() -> None:
    report = evaluate_terminal_holdout(
        strategy="today",
        research_identity="score_today_historical_candidate_v1",
        parent_hash="a" * 64,
        candidate_hash="b" * 64,
        rows=(),
        terminal_holdout_already_opened=True,
    )

    assert report.terminal_holdout_opened is False
    assert report.status == "historical_rejected"
    assert report.failure_reasons == ("terminal_holdout_already_opened",)
