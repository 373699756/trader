"""Risk fact derivation, de-duplication and deterministic penalty mapping."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime

from trader.domain.factors import clamp
from trader.domain.models import FeatureSnapshot, RiskFact, RiskRule


def derive_local_risk_facts(
    snapshot: FeatureSnapshot,
    observed_at: datetime,
    rules: Mapping[str, RiskRule],
) -> tuple[RiskFact, ...]:
    facts = list(snapshot.external_risk_facts)
    checks = (
        ("near_limit_crowding", snapshot.value("limit_proximity", 0.0) >= 0.75),
        ("price_volume_divergence", snapshot.value("price_volume_divergence", 0.0) >= 0.5),
        ("high_volatility", snapshot.value("volatility_20d", 0.0) >= 4.0),
        ("financial_deterioration", snapshot.value("financial_deterioration", 0.0) >= 0.5),
    )
    for risk_code, triggered in checks:
        if triggered and risk_code in rules:
            facts.append(_fact_from_rule(snapshot.quote.code, rules[risk_code], observed_at, "local_factor"))

    _append_severity_fact(facts, snapshot, observed_at, rules, "reduction_or_unlock", "reduction_or_unlock")
    _append_severity_fact(facts, snapshot, observed_at, rules, "pledge_risk", "pledge_risk")
    announcement_level = int(snapshot.value("negative_announcement_level", 0.0))
    if announcement_level >= 3 and "regulatory_risk" in rules:
        facts.append(_fact_from_rule(snapshot.quote.code, rules["regulatory_risk"], observed_at, "event"))
    elif announcement_level >= 1 and "negative_announcement" in rules:
        facts.append(_fact_from_rule(snapshot.quote.code, rules["negative_announcement"], observed_at, "event"))
    return deduplicate_risk_facts(facts)


def map_deepseek_risk_facts(
    raw_facts: Iterable[RiskFact],
    rules: Mapping[str, RiskRule],
    local_fact_ids: set[str],
    *,
    cap: float,
) -> tuple[tuple[RiskFact, ...], float, bool]:
    mapped: list[RiskFact] = []
    veto = False
    for raw in raw_facts:
        rule = rules.get(raw.risk_code)
        if rule is None or raw.confidence < rule.minimum_confidence or not raw.evidence_ids:
            continue
        if raw.risk_fact_id in local_fact_ids:
            veto = veto or raw.veto
            continue
        mapped_fact = RiskFact(
            risk_fact_id=raw.risk_fact_id,
            risk_code=rule.risk_code,
            severity=rule.severity,
            penalty=rule.penalty,
            source=raw.source,
            observed_at=raw.observed_at,
            confidence=raw.confidence,
            evidence_ids=raw.evidence_ids,
            group=rule.group,
            veto=raw.veto,
        )
        mapped.append(mapped_fact)
        veto = veto or mapped_fact.veto
    deduplicated = deduplicate_risk_facts(mapped)
    return deduplicated, aggregate_risk_penalty(deduplicated, cap=cap), veto


def deduplicate_risk_facts(facts: Iterable[RiskFact]) -> tuple[RiskFact, ...]:
    by_id: dict[str, RiskFact] = {}
    for fact in facts:
        current = by_id.get(fact.risk_fact_id)
        if current is None or fact.penalty > current.penalty:
            by_id[fact.risk_fact_id] = fact
    by_group: dict[str, RiskFact] = {}
    independent: list[RiskFact] = []
    for fact in by_id.values():
        if not fact.group:
            independent.append(fact)
            continue
        current = by_group.get(fact.group)
        if current is None or fact.penalty > current.penalty:
            by_group[fact.group] = fact
    return tuple(sorted((*independent, *by_group.values()), key=lambda fact: (fact.risk_code, fact.risk_fact_id)))


def aggregate_risk_penalty(facts: Iterable[RiskFact], *, cap: float) -> float:
    return clamp(sum(max(0.0, fact.penalty) for fact in facts), 0.0, cap)


def _append_severity_fact(
    facts: list[RiskFact],
    snapshot: FeatureSnapshot,
    observed_at: datetime,
    rules: Mapping[str, RiskRule],
    value_name: str,
    rule_prefix: str,
) -> None:
    level = int(snapshot.value(value_name, 0.0))
    suffix = {1: "low", 2: "medium", 3: "high"}.get(min(3, max(0, level)))
    risk_code = f"{rule_prefix}_{suffix}" if suffix else ""
    if risk_code and risk_code in rules:
        facts.append(_fact_from_rule(snapshot.quote.code, rules[risk_code], observed_at, "local_factor"))


def _fact_from_rule(code: str, rule: RiskRule, observed_at: datetime, source: str) -> RiskFact:
    return RiskFact(
        risk_fact_id=f"{code}:{rule.risk_code}:{observed_at.date().isoformat()}",
        risk_code=rule.risk_code,
        severity=rule.severity,
        penalty=rule.penalty,
        source=source,
        observed_at=observed_at,
        group=rule.group,
    )


__all__ = [
    "aggregate_risk_penalty",
    "deduplicate_risk_facts",
    "derive_local_risk_facts",
    "map_deepseek_risk_facts",
]
