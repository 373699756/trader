from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from trader.domain.models import RiskFact, RiskRule, Strategy
from trader.domain.risk import aggregate_risk_penalty, deduplicate_risk_facts, derive_local_risk_facts


def _rule(
    code: str,
    factor: str,
    operator: str,
    thresholds: tuple[float, ...],
    penalty: float,
    *,
    group: str = "independent",
    mode: str = "additive",
    strategies: tuple[str, ...] = ("today",),
) -> RiskRule:
    return RiskRule(
        code,
        "medium",
        penalty,
        0.7,
        group,
        strategies=strategies,
        trigger_factor=factor,
        trigger_operator=operator,
        trigger_thresholds=thresholds,
        combination_mode=mode,
        risk_fact_id_fields=("stock_code", "risk_code", "actual", "source", "trade_date"),
    )


def test_local_risk_is_config_driven_auditable_and_stable(application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(feature, values={**feature.values, "custom_risk": 0.75})
    rule = _rule("configured_risk", "custom_risk", "gte", (0.75,), 5.0)

    first = derive_local_risk_facts(feature, now, {rule.risk_code: rule}, strategy=Strategy.TODAY)
    later = now + timedelta(minutes=5)
    refreshed = replace(feature, observed_at=later)
    second = derive_local_risk_facts(refreshed, later, {rule.risk_code: rule}, strategy=Strategy.TODAY)

    assert first[0].risk_fact_id == second[0].risk_fact_id
    assert first[0].risk_fact_id.startswith("risk_")
    assert first[0].actual == 0.75
    assert first[0].threshold == ">= 0.75"
    assert first[0].source == feature.quote.source
    assert first[0].observed_at == feature.observed_at


def test_expired_local_evidence_does_not_trigger(application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    feature = application_feature_factory("600001", now)
    feature = replace(feature, values={**feature.values, "custom_risk": 1.0})
    rule = replace(_rule("configured_risk", "custom_risk", "gte", (0.5,), 5.0), evidence_ttl_hours=1)

    facts = derive_local_risk_facts(
        feature,
        now + timedelta(hours=1, seconds=1),
        {rule.risk_code: rule},
        strategy=Strategy.TODAY,
    )

    assert facts == ()


def test_missing_non_finite_and_wrong_strategy_do_not_trigger(application_feature_factory) -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    rule = _rule("configured_risk", "custom_risk", "gte", (0.5,), 5.0)
    base = application_feature_factory("600001", now)
    missing = replace(base, values={**base.values, "custom_risk": None})
    non_finite = replace(base, values={**base.values, "custom_risk": float("nan")})
    present = replace(base, values={**base.values, "custom_risk": 1.0})

    assert derive_local_risk_facts(missing, now, {rule.risk_code: rule}, strategy=Strategy.TODAY) == ()
    assert derive_local_risk_facts(non_finite, now, {rule.risk_code: rule}, strategy=Strategy.TODAY) == ()
    assert derive_local_risk_facts(present, now, {rule.risk_code: rule}, strategy=Strategy.LONG) == ()


def test_exclusive_groups_keep_highest_while_additive_rules_stack() -> None:
    now = datetime.fromisoformat("2026-07-16T10:00:00+08:00")
    low = RiskFact("low", "low", "low", 3.0, "fixture", now, group="event")
    high = RiskFact("high", "high", "high", 8.0, "fixture", now, group="event")
    extra = RiskFact("extra", "extra", "low", 20.0, "fixture", now, group="structure")
    rules = {
        "low": _rule("low", "x", "eq", (1.0,), 3.0, group="event", mode="exclusive"),
        "high": _rule("high", "x", "eq", (2.0,), 8.0, group="event", mode="exclusive"),
        "extra": _rule("extra", "x", "eq", (3.0,), 20.0, group="structure", mode="additive"),
    }

    facts = deduplicate_risk_facts((low, high, extra, high), rules=rules)

    assert {fact.risk_fact_id for fact in facts} == {"high", "extra"}
    assert sum(fact.penalty for fact in facts) == 28.0
    assert aggregate_risk_penalty(facts, cap=25.0) == 25.0
