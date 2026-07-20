"""Risk fact derivation, de-duplication and deterministic penalty mapping."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import Enum

from trader.domain.factors import clamp
from trader.domain.models import Evidence, FeatureSnapshot, RiskFact, RiskRule, Strategy


class Rating(str, Enum):
    """Explicit mapping from DeepSeek free-text assessment to local action labels.

    Used to bridge unstructured model output with deterministic local decisions.
    The fallback is always ``NEUTRAL`` — missing or unparseable text must not
    be silently promoted.
    """

    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


def parse_rating(text: str, *, fallback: Rating = Rating.NEUTRAL) -> Rating:
    """Parse a free-text rating string into a ``Rating`` enum.

    Only exact known labels are accepted; unknown or empty strings return
    *fallback*.
    """
    if not text or not text.strip():
        return fallback
    normalized = text.strip().lower()
    if normalized in {"bullish", "看多", "积极"}:
        return Rating.BULLISH
    if normalized in {"bearish", "看空", "消极"}:
        return Rating.BEARISH
    if normalized in {"neutral", "中性"}:
        return Rating.NEUTRAL
    return fallback


def derive_local_risk_facts(
    snapshot: FeatureSnapshot,
    observed_at: datetime,
    rules: Mapping[str, RiskRule],
    *,
    strategy: Strategy,
) -> tuple[RiskFact, ...]:
    facts = list(snapshot.external_risk_facts)
    for rule in rules.values():
        if not rule.local_trigger_enabled:
            continue
        if rule.strategies and strategy.value not in rule.strategies:
            continue
        age_seconds = (observed_at - snapshot.observed_at).total_seconds()
        if age_seconds < 0 or age_seconds > rule.evidence_ttl_hours * 3600:
            continue
        actual = snapshot.optional_value(rule.trigger_factor)
        if actual is None or not _triggered(actual, rule):
            continue
        facts.append(
            _fact_from_rule(
                snapshot.quote.code,
                rule,
                snapshot.observed_at,
                snapshot.quote.source,
                actual,
            )
        )
    return deduplicate_risk_facts(facts, rules=rules)


def map_deepseek_risk_facts(
    raw_facts: Iterable[RiskFact],
    rules: Mapping[str, RiskRule],
    local_fact_ids: set[str],
    *,
    cap: float,
    evidence: Iterable[Evidence],
    evaluated_at: datetime,
) -> tuple[tuple[RiskFact, ...], float, bool]:
    mapped: list[RiskFact] = []
    veto = False
    evidence_by_id = {item.evidence_id: item for item in evidence}
    for raw in raw_facts:
        rule = rules.get(raw.risk_code)
        if (
            rule is None
            or raw.confidence < rule.minimum_confidence
            or not raw.evidence_ids
            or not _evidence_is_valid(raw.evidence_ids, rule, evidence_by_id, evaluated_at)
        ):
            continue
        locally_mapped_veto = (
            rule.veto and rule.severity == "high" and raw.confidence >= max(0.7, rule.minimum_confidence)
        )
        if raw.risk_fact_id in local_fact_ids:
            veto = veto or locally_mapped_veto
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
            veto=locally_mapped_veto,
            threshold=raw.threshold,
            actual=raw.actual,
            assessment=raw.assessment,
        )
        mapped.append(mapped_fact)
        veto = veto or mapped_fact.veto
    deduplicated = deduplicate_risk_facts(mapped, rules=rules)
    return deduplicated, aggregate_risk_penalty(deduplicated, cap=cap), veto


def _evidence_is_valid(
    evidence_ids: tuple[str, ...],
    rule: RiskRule,
    evidence_by_id: Mapping[str, Evidence],
    evaluated_at: datetime,
) -> bool:
    maximum_age_seconds = rule.evidence_ttl_hours * 3600
    for evidence_id in evidence_ids:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            return False
        if rule.allowed_evidence_types and item.evidence_type not in rule.allowed_evidence_types:
            return False
        if item.published_at.tzinfo is None or item.published_at.utcoffset() is None:
            return False
        age_seconds = (evaluated_at - item.published_at).total_seconds()
        if age_seconds < 0 or age_seconds > maximum_age_seconds:
            return False
    return True


def deduplicate_risk_facts(
    facts: Iterable[RiskFact],
    *,
    rules: Mapping[str, RiskRule] | None = None,
) -> tuple[RiskFact, ...]:
    by_id: dict[str, RiskFact] = {}
    for fact in facts:
        current = by_id.get(fact.risk_fact_id)
        if current is None or fact.penalty > current.penalty:
            by_id[fact.risk_fact_id] = fact
    by_group: dict[str, RiskFact] = {}
    independent: list[RiskFact] = []
    for fact in by_id.values():
        rule = (rules or {}).get(fact.risk_code)
        if not fact.group or (rule is not None and rule.combination_mode == "additive"):
            independent.append(fact)
            continue
        current = by_group.get(fact.group)
        if current is None or fact.penalty > current.penalty:
            by_group[fact.group] = fact
    return tuple(sorted((*independent, *by_group.values()), key=lambda fact: (fact.risk_code, fact.risk_fact_id)))


def aggregate_risk_penalty(facts: Iterable[RiskFact], *, cap: float) -> float:
    return clamp(sum(max(0.0, fact.penalty) for fact in facts), 0.0, cap)


def _triggered(actual: float, rule: RiskRule) -> bool:
    if not math.isfinite(actual):
        return False
    thresholds = rule.trigger_thresholds
    if rule.trigger_operator == "gte":
        return actual >= thresholds[0]
    if rule.trigger_operator == "eq":
        return actual == thresholds[0]
    if rule.trigger_operator == "gte_lt":
        return thresholds[0] <= actual < thresholds[1]
    raise ValueError(f"unsupported risk trigger operator: {rule.trigger_operator}")


def _threshold_text(rule: RiskRule) -> str:
    if rule.trigger_operator == "gte":
        return f">= {rule.trigger_thresholds[0]:g}"
    if rule.trigger_operator == "eq":
        return f"== {rule.trigger_thresholds[0]:g}"
    if rule.trigger_operator == "gte_lt":
        return f">= {rule.trigger_thresholds[0]:g} and < {rule.trigger_thresholds[1]:g}"
    raise ValueError(f"unsupported risk trigger operator: {rule.trigger_operator}")


def _fact_from_rule(
    code: str,
    rule: RiskRule,
    observed_at: datetime,
    source: str,
    actual: float,
) -> RiskFact:
    values: dict[str, object] = {
        "stock_code": code,
        "risk_code": rule.risk_code,
        "actual": actual,
        "source": source,
        "observed_at": observed_at.isoformat(),
        "trade_date": observed_at.date().isoformat(),
    }
    identity = {field: values[field] for field in rule.risk_fact_id_fields}
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]
    return RiskFact(
        risk_fact_id=f"risk_{digest}",
        risk_code=rule.risk_code,
        severity=rule.severity,
        penalty=rule.penalty,
        source=source,
        observed_at=observed_at,
        group=rule.group,
        veto=rule.veto,
        threshold=_threshold_text(rule),
        actual=actual,
    )


__all__ = [
    "Rating",
    "aggregate_risk_penalty",
    "deduplicate_risk_facts",
    "derive_local_risk_facts",
    "map_deepseek_risk_facts",
    "parse_rating",
]
