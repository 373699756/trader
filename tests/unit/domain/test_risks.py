from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import RiskRule
from trader.domain.review.rules import aggregate_risk_penalty, derive_local_risk_facts

PENALTIES = {
    "near_limit_crowding": 5.0,
    "price_volume_divergence": 4.0,
    "high_volatility": 3.0,
    "short_term_overheat": 3.0,
    "intraday_reversal": 4.0,
    "liquidity_contraction": 3.0,
    "trend_breakdown": 3.0,
}


def _rules() -> dict[str, RiskRule]:
    return {
        code: RiskRule(
            code,
            "medium",
            penalty,
            0.7,
            f"group:{code}",
            strategies=("today", "tomorrow", "d25"),
            trigger_factor=code,
            trigger_operator="gte",
            trigger_thresholds=(0.5,),
            combination_mode="additive",
            risk_fact_id_fields=("stock_code", "risk_code", "actual", "source", "trade_date"),
        )
        for code, penalty in PENALTIES.items()
    }


def test_seven_short_risks_are_deduplicated_and_capped_at_25(application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(feature, values={**feature.values, **dict.fromkeys(PENALTIES, 0.5)})

    facts = derive_local_risk_facts(feature, now, _rules(), strategy=Strategy.TODAY)

    assert {fact.risk_code: fact.penalty for fact in facts} == PENALTIES
    assert len({fact.risk_fact_id for fact in (*facts, *facts)}) == 7
    assert aggregate_risk_penalty((*facts, *facts), cap=25.0) == 25.0
    assert derive_local_risk_facts(feature, now, _rules(), strategy=Strategy.LONG) == ()
