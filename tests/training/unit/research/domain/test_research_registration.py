"""Synthetic registrations preserve identity and count states by trading day."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Literal

import pytest

from tests.helpers.training.research.registrations import (
    CandidateRegistrationFixture, RegistrationPayloadFixture, StateRegistrationFixture,
)
from trader.recommendation.domain.scoring.profile_identity import ScoringProfileId
from trader.training.domain.evaluation.research_registration import ResearchParameter, ResearchStateRule, ResearchStrategy
from trader.training.domain.evaluation.terminal_holdout import (
    EvaluateTerminalHoldout, TerminalHoldoutEvaluation, TerminalHoldoutRow,
)
from trader.training.infra.research.registration_codec import DecodeCandidateRegistration, DecodeStateRegistration


@pytest.mark.parametrize("profile_id", ["v1", "v2", "v3", "v5"])
@pytest.mark.parametrize("strategy", ["tomorrow", "d25"])
def test_all_profile_strategy_registrations_round_trip_without_changing_hash(
    profile_id: ScoringProfileId, strategy: ResearchStrategy,
) -> None:
    registration = CandidateRegistrationFixture(profile_id, strategy, fit_dates=61 if profile_id == "v5" else 750)
    decoded = DecodeCandidateRegistration(RegistrationPayloadFixture(registration))
    assert decoded == registration
    assert decoded.content_hash == registration.content_hash
    assert decoded.profile_id == profile_id and decoded.strategy == strategy
    assert all(type(parameter.value) is float for parameter in decoded.parameters)
    if profile_id == "v1":
        assert tuple((item.role, item.family) for item in decoded.candidate_components) == (("ranker", "lambdarank"), ("utility", "ridge"))
    if profile_id == "v5":
        assert decoded.candidate_components[0].sequence_length == 60
        assert decoded.candidate_components[0].epochs == 10


def test_integer_and_float_candidate_parameters_have_one_canonical_identity() -> None:
    registration = CandidateRegistrationFixture()
    integral = replace(registration, parameters=(ResearchParameter("fit_dates", 750, "trading_days"),))
    floating = replace(registration, parameters=(ResearchParameter("fit_dates", 750.0, "trading_days"),))
    assert integral.content_hash == floating.content_hash
    assert DecodeCandidateRegistration(RegistrationPayloadFixture(integral)).content_hash == integral.content_hash


def test_integer_and_float_daily_state_values_and_thresholds_have_one_canonical_identity() -> None:
    state = StateRegistrationFixture((date(2025, 1, 1), date(2025, 1, 2)))
    integral = replace(
        state, market=replace(state.market, thresholds=(-1, 1)),
        volatility=replace(state.volatility, thresholds=(0,)), liquidity=replace(state.liquidity, thresholds=(1,)),
        days=tuple(replace(day, market_value=1, volatility_value=0, liquidity_value=1) for day in state.days),
    )
    floating = replace(
        state, market=replace(state.market, thresholds=(-1.0, 1.0)),
        volatility=replace(state.volatility, thresholds=(0.0,)), liquidity=replace(state.liquidity, thresholds=(1.0,)),
        days=tuple(replace(day, market_value=1.0, volatility_value=0.0, liquidity_value=1.0) for day in state.days),
    )
    assert integral.content_hash == floating.content_hash
    assert DecodeStateRegistration(RegistrationPayloadFixture(integral)).content_hash == integral.content_hash


@pytest.mark.parametrize(
    "boundary,value,label",
    [
        ("upper_inclusive", -0.500001, "down"), ("upper_inclusive", -0.5, "down"),
        ("upper_inclusive", -0.499999, "sideways"), ("upper_inclusive", 0.5, "sideways"),
        ("upper_inclusive", 0.500001, "up"), ("lower_inclusive", -0.500001, "down"),
        ("lower_inclusive", -0.5, "sideways"), ("lower_inclusive", 0.499999, "sideways"),
        ("lower_inclusive", 0.5, "up"), ("lower_inclusive", 0.500001, "up"),
    ],
)
def test_registered_state_threshold_boundary_is_exact(
    boundary: Literal["upper_inclusive", "lower_inclusive"], value: float, label: str,
) -> None:
    rule = ResearchStateRule("fixture_market", 20, (-0.5, 0.5), ("down", "sideways", "up"), boundary, unit="standardized_ratio")
    assert rule.classify(value) == label


def _state_rows() -> tuple[TerminalHoldoutRow, ...]:
    dates = tuple(date(2024, 1, 1) + timedelta(days=index) for index in range(200))
    registration = StateRegistrationFixture(dates, vary=True)
    result = []
    for day in registration.days:
        market, volatility, liquidity = registration.states(day)
        for code, board in (("600001", "main"), ("300001", "chinext"), ("688001", "star")):
            result.append(TerminalHoldoutRow(
                day.trade_date, code, board, "fixture_industry", market, volatility, liquidity,
                0.02, (0.018, 0.015, 0.01), (0.008, 0.005, 0.0),
                False, False, False, False, -0.1, -0.1, False, False, 10_000, 10_000, True,
                ranking_score=50.0, local_score=50.0,
            ))
    return tuple(result)


def _evaluation(rows: tuple[TerminalHoldoutRow, ...]) -> TerminalHoldoutEvaluation:
    return TerminalHoldoutEvaluation(
        "tomorrow", "score_tomorrow_historical_candidate", "a" * 64, "b" * 64, rows, profile_id="v2",
    )


def test_daily_state_counts_are_invariant_to_row_order_and_stock_multiplicity() -> None:
    rows = _state_rows()
    first = EvaluateTerminalHoldout(_evaluation(rows))
    second = EvaluateTerminalHoldout(_evaluation(tuple(reversed(rows))))
    assert first == second and first.content_hash == second.content_hash
    counts = dict(first.metrics.state_sample_counts)
    assert first.metrics.evaluated_rows == 600 and first.metrics.evaluated_trade_dates == 200
    assert counts["market:up"] == counts["market:down"] == 67
    assert counts["market:sideways"] == 66
    assert counts["volatility:low"] == counts["volatility:high"] == 100
    assert counts["liquidity:low"] == counts["liquidity:high"] == 100
    assert counts["board:main"] == counts["board:chinext"] == counts["board:star"] == 200


def test_mixed_states_for_the_same_trading_day_are_rejected() -> None:
    rows = _state_rows()
    mixed = (rows[0], replace(rows[1], market_state="down"), *rows[2:])
    with pytest.raises(ValueError, match="terminal_daily_state_mixed"):
        EvaluateTerminalHoldout(_evaluation(mixed))
