from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from trader.domain.market.models import Board
from trader.domain.recommendation.models import (
    BoardStrategyPolicy,
    Strategy,
)
from trader.domain.recommendation.scoring import (
    BoardCrossSectionRequest,
    apply_board_policy,
    build_board_cross_section,
    score_board_strategy,
    supported_weight,
)

NOW = datetime(2026, 7, 16, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _build_board_cross_section(
    features,
    *,
    board: Board,
    merge_epoch: str,
    trade_date: str,
    phase: str,
    data_version: str,
):
    return build_board_cross_section(
        BoardCrossSectionRequest(
            features=features,
            board=board,
            merge_epoch=merge_epoch,
            trade_date=trade_date,
            phase=phase,
            data_version=data_version,
        )
    )


def _policy(strategy: Strategy, board: Board, weights: dict[str, float]) -> BoardStrategyPolicy:
    candidate = {
        Strategy.TODAY: {
            "liquidity": 0.30,
            "intraday_structure": 0.25,
            "turnover_state": 0.20,
            "peer_gap": 0.15,
            "data_completeness": 0.10,
        },
        Strategy.TOMORROW: {
            "liquidity": 0.35,
            "peer_gap": 0.15,
            "trend": 0.25,
            "stability": 0.15,
            "data_completeness": 0.10,
        },
        Strategy.D25: {
            "liquidity": 0.30,
            "residual_momentum": 0.20,
            "trend": 0.20,
            "stability": 0.15,
            "execution": 0.10,
            "data_completeness": 0.05,
        },
    }[strategy]
    return BoardStrategyPolicy(
        policy_id=f"v16:{strategy.value}:{board.value}",
        version="v16",
        board=board,
        strategy=strategy,
        candidate_weights=candidate,
        local_weights=weights,
    )


def _features(application_feature_factory, count: int, board: Board):
    prefixes = {Board.MAIN: "600", Board.CHINEXT: "300", Board.STAR: "688"}
    result = []
    for index in range(count):
        feature = application_feature_factory(f"{prefixes[board]}{index:03d}", NOW, industry="同业")
        result.append(
            replace(
                feature,
                quote=replace(feature.quote, board=board, pct_change=float(index), turnover_rate=2.0),
                values={
                    **feature.values,
                    "return_3d": float(index),
                    "return_5d": float(index),
                    "return_20d": float(index),
                    "return_60d": float(index),
                    "turnover_median_20d": 1.0,
                },
            )
        )
    return tuple(result)


@pytest.mark.parametrize(("count", "expected"), [(10, None), (11, 100.0)])
def test_peer_population_excludes_target_at_nine_ten_peer_boundary(
    application_feature_factory,
    count: int,
    expected: float | None,
) -> None:
    cross_section = _build_board_cross_section(
        _features(application_feature_factory, count, Board.MAIN),
        board=Board.MAIN,
        merge_epoch="epoch-1",
        trade_date="2026-07-16",
        phase="today_main",
        data_version="data-1",
    )

    assert cross_section.features[-1].optional_value("peer_gap_5d_score") == expected


@pytest.mark.parametrize(("count", "present"), [(11, False), (16, True)])
def test_leader_group_excludes_target_at_two_three_leader_boundary(
    application_feature_factory,
    count: int,
    present: bool,
) -> None:
    cross_section = _build_board_cross_section(
        _features(application_feature_factory, count, Board.MAIN),
        board=Board.MAIN,
        merge_epoch="epoch-1",
        trade_date="2026-07-16",
        phase="afternoon",
        data_version="data-1",
    )

    leader_gap = cross_section.features[-1].optional_value("leader_gap")
    assert (leader_gap is not None) is present


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_shock_denominators_remain_missing(
    application_feature_factory,
    bad: float,
) -> None:
    features = _features(application_feature_factory, 11, Board.MAIN)
    first = replace(
        features[0],
        values={**features[0].values, "turnover_median_20d": bad, "amount_median_20d": bad},
    )
    cross_section = _build_board_cross_section(
        (first, *features[1:]),
        board=Board.MAIN,
        merge_epoch="epoch-1",
        trade_date="2026-07-16",
        phase="today_main",
        data_version="data-1",
    )

    assert cross_section.features[0].optional_value("turnover_shock_20") is None
    assert cross_section.features[0].optional_value("amount_shock_20") is None


def test_same_input_uses_board_specific_tomorrow_weights(application_feature_factory) -> None:
    feature = _features(application_feature_factory, 1, Board.MAIN)[0]
    values = {
        **feature.values,
        "peer_gap_5d_score": 100.0,
        "peer_gap_20d_score": 100.0,
        "leader_gap_score": 100.0,
        "turnover_shock_score": 0.0,
        "amount_shock_score": 0.0,
        "flow_confirmation_score": 0.0,
    }
    main_policy = _policy(
        Strategy.TOMORROW,
        Board.MAIN,
        {
            "tail_structure": 0.15,
            "peer_leader": 0.10,
            "turnover_flow": 0.05,
            "trend": 0.20,
            "stability": 0.25,
            "market_state": 0.10,
            "entry_quality": 0.15,
        },
    )
    growth_policy = _policy(
        Strategy.TOMORROW,
        Board.CHINEXT,
        {
            "tail_structure": 0.20,
            "peer_leader": 0.20,
            "turnover_flow": 0.15,
            "trend": 0.15,
            "stability": 0.10,
            "market_state": 0.05,
            "entry_quality": 0.15,
        },
    )
    main = replace(feature, values=values, board_policy_id=main_policy.policy_id)
    growth = replace(main, quote=replace(main.quote, board=Board.CHINEXT), board_policy_id=growth_policy.policy_id)

    assert score_board_strategy(main, main_policy).base_score != score_board_strategy(growth, growth_policy).base_score


def test_d25_score_has_no_market_regime_or_overheat_multiplier(application_feature_factory) -> None:
    policy = _policy(
        Strategy.D25,
        Board.MAIN,
        {
            "residual_momentum": 0.15,
            "trend": 0.25,
            "quality_value": 0.25,
            "stability": 0.15,
            "flow_liquidity": 0.10,
            "entry_quality": 0.10,
        },
    )
    feature = replace(_features(application_feature_factory, 1, Board.MAIN)[0], market_regime="risk_on")
    cross_section = _build_board_cross_section(
        (feature,),
        board=Board.MAIN,
        merge_epoch="epoch",
        trade_date="2026-07-16",
        phase="afternoon",
        data_version="data",
    )
    scored = apply_board_policy(cross_section, Strategy.D25, policy)[0]

    risk_on = score_board_strategy(scored, policy)
    risk_off = score_board_strategy(replace(scored, market_regime="risk_off"), policy)

    assert risk_on == risk_off


def test_supported_weight_tracks_known_inputs_within_component() -> None:
    supported = supported_weight(
        Strategy.D25,
        {
            "quality_score": 70.0,
            "value_score": 70.0,
            "growth_score": None,
        },
        {"quality_value": 1.0},
    )

    assert supported == pytest.approx(2.0 / 3.0)


def test_supported_weight_treats_industry_trend_as_optional_overlay() -> None:
    supported = supported_weight(
        Strategy.TOMORROW,
        {
            "ma20_60_position": 70.0,
            "ma_slope": 70.0,
            "breakout_20d": 70.0,
            "industry_trend": None,
        },
        {"trend": 1.0},
    )

    assert supported == 1.0


def test_close_fallback_reliability_excludes_unavailable_intraday_tail() -> None:
    values = {
        "tail_return_30m": None,
        "tail_volume_ratio": None,
        "close_location": None,
        "peer_gap_5d_score": 70.0,
        "peer_gap_20d_score": 70.0,
        "leader_gap_score": 70.0,
        "turnover_shock_score": 70.0,
        "amount_shock_score": 70.0,
        "flow_confirmation_score": 70.0,
        "ma20_60_position": 70.0,
        "ma_slope": 70.0,
        "breakout_20d": 70.0,
        "low_volatility_score": 70.0,
        "low_drawdown_score": 70.0,
        "entry_quality": 70.0,
    }
    weights = {
        "tail_structure": 0.15,
        "peer_leader": 0.10,
        "turnover_flow": 0.05,
        "trend": 0.20,
        "stability": 0.25,
        "market_state": 0.10,
        "entry_quality": 0.15,
    }

    assert supported_weight(Strategy.TOMORROW, values, weights) == pytest.approx(0.85)
    assert (
        supported_weight(
            Strategy.TOMORROW,
            values,
            weights,
            phase="close_fallback",
        )
        == 1.0
    )
