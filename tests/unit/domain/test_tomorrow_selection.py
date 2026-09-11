from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from trader.domain.market.models import Board
from trader.domain.recommendation.models import BoardStrategyPolicy, ScoredDisposition, Strategy
from trader.domain.recommendation.selection.scored_selection import (
    ScoredSelectionPolicy,
    ScoredSelectionRequest,
    plan_scored_candidates,
    select_scored,
)
from trader.domain.review.models import RiskRule

NOW = datetime(2026, 7, 28, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))

_CANDIDATE_WEIGHTS = {
    "liquidity": 0.4,
    "trend": 0.3,
    "stability": 0.2,
    "data_completeness": 0.1,
}
_LOCAL_WEIGHTS = {
    "tail_structure": 0.2,
    "turnover_flow": 0.1,
    "trend": 0.2,
    "stability": 0.2,
    "market_state": 0.1,
    "entry_quality": 0.2,
}
_CANDIDATE_COMPONENT_WEIGHTS = {
    "stability": {"low_volatility_score": 0.5, "low_drawdown_score": 0.5},
}
_LOCAL_COMPONENT_WEIGHTS = {
    "tail_structure": {"tail_return_30m": 0.35, "tail_volume_ratio": 0.3, "close_location": 0.35},
    "turnover_flow": {
        "turnover_shock_score": 0.35,
        "amount_shock_score": 0.35,
        "flow_confirmation_score": 0.3,
    },
    "trend": {"ma20_60_position": 0.375, "ma_slope": 0.375, "breakout_20d": 0.25},
    "stability": {"low_volatility_score": 0.5, "low_drawdown_score": 0.5},
}


def _board_policy(board: Board) -> BoardStrategyPolicy:
    return BoardStrategyPolicy(
        policy_id=f"tomorrow-policy:{board.value}",
        version="tomorrow-policy",
        board=board,
        strategy=Strategy.TOMORROW,
        candidate_weights=_CANDIDATE_WEIGHTS,
        local_weights=_LOCAL_WEIGHTS,
        candidate_component_weights=_CANDIDATE_COMPONENT_WEIGHTS,
        local_component_weights=_LOCAL_COMPONENT_WEIGHTS,
    )


def _selection_policy(*, candidate_limit: int = 120, top_k: int = 10) -> ScoredSelectionPolicy:
    return ScoredSelectionPolicy(
        board_policies={
            Board.MAIN: _board_policy(Board.MAIN),
            Board.CHINEXT: _board_policy(Board.CHINEXT),
            Board.STAR: _board_policy(Board.STAR),
        },
        risk_rules={
            "price_volume_divergence": RiskRule(
                risk_code="price_volume_divergence",
                severity="medium",
                penalty=5.0,
                minimum_confidence=0.7,
                group="market_structure",
                strategies=("tomorrow",),
                trigger_factor="price_volume_divergence",
                trigger_operator="gte",
                trigger_thresholds=(1.0,),
                veto=True,
            )
        },
        max_age_seconds=60.0,
        local_risk_cap=25.0,
        candidate_limit_per_board=candidate_limit,
        top_k=top_k,
        maximum_per_industry=2,
        minimum_local_score=0.0,
    )


def _features(application_feature_factory, count: int = 125):
    result = []
    for index in range(count):
        code = f"600{index:03d}"
        feature = application_feature_factory(code, NOW, industry=f"industry-{index}")
        result.append(
            replace(
                feature,
                quote=replace(
                    feature.quote,
                    board=Board.MAIN,
                    board_source="security_master",
                    board_reliability="verified",
                    listing_age_sessions=100,
                ),
                values={
                    **feature.values,
                    "return_3d": float(index),
                    "return_5d": float(index),
                    "volatility_20d": 2.0 + index / 1000,
                    "max_drawdown_20d": -5.0 - index / 1000,
                    "turnover_median_20d": 1.0,
                    "tail_return_30m": 60.0 + index / 10,
                },
            )
        )
    return tuple(result)


def _request(features, policy: ScoredSelectionPolicy) -> ScoredSelectionRequest:
    return ScoredSelectionRequest(
        features=features,
        evaluated_at=NOW,
        trade_date=NOW.date().isoformat(),
        phase="tomorrow",
        data_version="market-1",
        merge_epoch="market:2026-07-28:1",
        policy=policy,
    )


def test_tomorrow_selection_keeps_filter_tristate_and_bounds_each_board(
    application_feature_factory,
) -> None:
    features = list(_features(application_feature_factory))
    features[0] = replace(features[0], quote=replace(features[0].quote, is_st=True))
    features[1] = replace(
        features[1],
        quote=replace(
            features[1].quote,
            cross_source_deviation_pct=0.6,
            cross_source_verified=False,
        ),
    )
    features[2] = replace(
        features[2],
        values={
            **features[2].values,
            "trend_score": None,
            "volatility_20d": None,
        },
    )
    features[3] = replace(
        features[3],
        quote=replace(
            features[3].quote,
            execution_restrictions=("history_data_degraded",),
        ),
    )

    result = select_scored(_request(tuple(reversed(features)), _selection_policy()))
    by_code = {item.code: item for item in result.evaluations}

    assert by_code["600000"].disposition is ScoredDisposition.REJECT
    assert tuple(reason.code for reason in by_code["600000"].filter_reasons) == ("st_or_delisting",)
    assert by_code["600001"].disposition is ScoredDisposition.OBSERVE_ONLY
    assert tuple(flag.code for flag in by_code["600001"].optional_flags) == ("cross_source_deviation",)
    assert by_code["600002"].selection_skip_reason == "candidate_core_missing"
    assert by_code["600003"].disposition is ScoredDisposition.OBSERVE_ONLY
    assert len(result.scored_candidates) == 120
    assert tuple(item.code for item in result.evaluations) == tuple(sorted(item.code for item in result.evaluations))


def test_tomorrow_selection_deducts_local_risk_once_and_is_stable(
    application_feature_factory,
) -> None:
    features = list(_features(application_feature_factory, count=105))
    features[-1] = replace(
        features[-1],
        quote=replace(features[-1].quote, industry="concentrated"),
        values={**features[-1].values, "price_volume_divergence": 2.0},
    )
    features[-2] = replace(features[-2], quote=replace(features[-2].quote, industry="concentrated"))
    features[-3] = replace(features[-3], quote=replace(features[-3].quote, industry="concentrated"))
    policy = _selection_policy()

    forward = select_scored(_request(tuple(features), policy))
    reverse = select_scored(_request(tuple(reversed(features)), policy))
    risky = next(item for item in forward.scored_candidates if item.code == "600104")

    assert risky.local_risk_penalty == 5.0
    assert risky.local_score == round(risky.local_base_score - 5.0, 2)
    assert risky.disposition is ScoredDisposition.OBSERVE_ONLY
    assert risky.selection_skip_reason == "local_risk_veto"
    assert tuple(item.code for item in forward.selected) == tuple(item.code for item in reverse.selected)
    assert len(forward.selected) == 10
    assert sum(item.features.quote.industry == "concentrated" for item in forward.selected) <= 2
    assert [item.rank for item in forward.selected] == list(range(1, 11))


def test_production_model_ineligible_candidate_does_not_consume_board_limit(
    application_feature_factory,
) -> None:
    features = _features(application_feature_factory, count=2)
    request = replace(
        _request(features, _selection_policy(candidate_limit=1, top_k=1)),
        model_input_eligible_codes=frozenset({"600001"}),
    )

    result = select_scored(request)
    by_code = {item.code: item for item in result.evaluations}

    assert tuple(item.code for item in result.scored_candidates) == ("600001",)
    assert by_code["600000"].selection_skip_reason == "production_model_features_missing"


def test_candidate_stage_counts_follow_issuer_and_dynamic_qualification_before_cap(
    application_feature_factory,
) -> None:
    features = list(_features(application_feature_factory, count=5))
    features[0] = replace(features[0], quote=replace(features[0].quote, is_st=True))
    features[1] = replace(features[1], quote=replace(features[1].quote, is_suspended=True))
    features[2] = replace(features[2], history_days=10)
    features[3] = replace(
        features[3],
        values={
            **features[3].values,
            "trend_score": None,
            "volatility_20d": None,
            "max_drawdown_20d": None,
        },
    )

    plan = plan_scored_candidates(
        replace(
            _request(tuple(features), _selection_policy(candidate_limit=1)),
            minimum_history_sessions=20,
        )
    )

    assert plan.stage_counts.issuer_eligible_population == 4
    assert plan.stage_counts.dynamic_filter_eligible == 3
    assert plan.stage_counts.strategy_history_eligible == 2
    assert plan.stage_counts.model_input_eligible == 2
    assert plan.stage_counts.candidate_score_eligible == 1
    assert plan.stage_counts.candidate_limit_selected == 1


def test_every_required_candidate_rejection_is_applied_before_the_board_cap(
    application_feature_factory,
) -> None:
    def feature(code: str):
        value = application_feature_factory(code, NOW, industry=f"industry-{code}")
        return replace(
            value,
            quote=replace(
                value.quote,
                board=Board.MAIN,
                board_source="security_master",
                board_reliability="verified",
                listing_age_sessions=100,
            ),
        )

    unsupported = feature("900001")
    st = feature("600001")
    delisting = feature("600002")
    suspended = feature("600003")
    invalid_price = feature("600004")
    invalid_amount = feature("600005")
    stale = feature("600006")
    too_new = feature("600007")
    one_price = feature("600008")
    no_liquidity_history = feature("600009")
    no_strategy_history = feature("600010")
    no_model_input = feature("600011")
    core_missing = feature("600012")
    eligible = tuple(feature(code) for code in ("600100", "600101", "600102"))
    rejected = (
        replace(unsupported, quote=replace(unsupported.quote, board=Board.UNSUPPORTED)),
        replace(st, quote=replace(st.quote, is_st=True)),
        replace(delisting, quote=replace(delisting.quote, name="退市样本")),
        replace(suspended, quote=replace(suspended.quote, is_suspended=True)),
        replace(invalid_price, quote=replace(invalid_price.quote, price=None)),
        replace(invalid_amount, quote=replace(invalid_amount.quote, amount=None)),
        replace(stale, quote=replace(stale.quote, source_time=NOW - timedelta(seconds=61))),
        replace(too_new, quote=replace(too_new.quote, listing_age_sessions=5)),
        replace(one_price, quote=replace(one_price.quote, is_one_price_limit=True)),
        replace(no_liquidity_history, values={**no_liquidity_history.values, "amount_median_20d": None}),
        replace(no_strategy_history, history_days=19),
        no_model_input,
        replace(
            core_missing,
            values={
                **core_missing.values,
                "trend_score": None,
                "volatility_20d": None,
                "max_drawdown_20d": None,
            },
        ),
    )
    features = (*rejected, *eligible)
    request = replace(
        _request(features, _selection_policy(candidate_limit=3)),
        minimum_history_sessions=20,
        model_input_eligible_codes=frozenset(
            item.quote.code for item in features if item.quote.code != no_model_input.quote.code
        ),
    )

    plan = plan_scored_candidates(request)
    by_code = {item.code: item for item in plan.evaluations}

    assert plan.limited_codes(3) == tuple(item.quote.code for item in eligible)
    assert {reason.code for reason in by_code[unsupported.quote.code].filter_reasons} == {"unsupported_code"}
    assert {reason.code for reason in by_code[st.quote.code].filter_reasons} == {"st_or_delisting"}
    assert {reason.code for reason in by_code[delisting.quote.code].filter_reasons} == {"st_or_delisting"}
    assert {reason.code for reason in by_code[suspended.quote.code].filter_reasons} == {"suspended"}
    assert {reason.code for reason in by_code[invalid_price.quote.code].filter_reasons} == {"invalid_price"}
    assert {reason.code for reason in by_code[invalid_amount.quote.code].filter_reasons} == {"invalid_amount"}
    assert {reason.code for reason in by_code[stale.quote.code].filter_reasons} == {"stale_quote"}
    assert {reason.code for reason in by_code[too_new.quote.code].filter_reasons} == {"new_listing_session"}
    assert {reason.code for reason in by_code[one_price.quote.code].filter_reasons} == {"one_price_limit"}
    assert {reason.code for reason in by_code[no_liquidity_history.quote.code].filter_reasons} == {
        "missing_liquidity_history"
    }
    assert by_code[no_strategy_history.quote.code].selection_skip_reason == "strategy_history_insufficient"
    assert by_code[no_model_input.quote.code].selection_skip_reason == "production_model_features_missing"
    assert by_code[core_missing.quote.code].selection_skip_reason == "candidate_core_missing"


def test_st_and_suspended_rough_leaders_do_not_displace_the_next_three_eligible_codes(
    application_feature_factory,
) -> None:
    features = list(_features(application_feature_factory, count=5))
    features[0] = replace(features[0], quote=replace(features[0].quote, is_st=True))
    features[1] = replace(features[1], quote=replace(features[1].quote, is_suspended=True))

    request = _request(tuple(features), _selection_policy(candidate_limit=3))
    plan = plan_scored_candidates(request)
    repeated = plan_scored_candidates(replace(request, features=tuple(reversed(features))))

    assert plan.limited_codes(3) == ("600002", "600003", "600004")
    assert repeated.limited_codes(3) == plan.limited_codes(3)


def test_tomorrow_selection_records_local_threshold_exclusion(application_feature_factory) -> None:
    policy = replace(_selection_policy(top_k=1), minimum_local_score=100.0)

    result = select_scored(_request(_features(application_feature_factory, count=100), policy))

    assert result.selected == ()
    assert {
        item.selection_skip_reason for item in result.scored_candidates if item.disposition is ScoredDisposition.PASS
    } == {"local_score_below_minimum"}


def test_tomorrow_selection_uses_full_population_but_scores_only_explicit_candidates(
    application_feature_factory,
) -> None:
    population = _features(application_feature_factory, count=100)
    candidate = replace(
        population[-1],
        values={**population[-1].values, "tail_return_30m": 99.0},
        merge_epoch="candidate:explicit",
    )
    request = replace(
        _request(population, _selection_policy()),
        candidate_features=(candidate,),
    )

    result = select_scored(request)

    assert len(result.evaluations) == 100
    assert tuple(item.code for item in result.scored_candidates) == (candidate.quote.code,)
    assert result.scored_candidates[0].features.values["tail_return_30m"] == 99.0
    assert result.scored_candidates[0].features.merge_epoch == request.merge_epoch
    assert result.population_versions


def test_tomorrow_selection_rejects_feature_observed_after_evaluation(application_feature_factory) -> None:
    feature = replace(
        _features(application_feature_factory, count=1)[0],
        observed_at=NOW + timedelta(seconds=1),
    )

    try:
        select_scored(_request((feature,), _selection_policy()))
    except ValueError as exc:
        assert str(exc) == "scored selection cannot use future features"
    else:
        raise AssertionError("future feature batch must be rejected")
