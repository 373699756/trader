"""Cross-field evidence invariants for DeepSeek research output."""

from __future__ import annotations

from typing import Dict


UNKNOWN_VALUES = {None, "", "unknown", "未知", "unavailable", "数据不足"}


def availability_invariant_error(
    raw: Dict[str, object],
    candidate: Dict[str, object],
) -> str:
    availability = (
        candidate.get("data_availability")
        or candidate.get("research_data_availability")
        or candidate.get("availability")
        or {}
    )
    value_quality = raw.get("value_quality") or {}
    financial_health = raw.get("financial_health") or {}
    market_flow = raw.get("market_flow") or {}
    industry_policy = raw.get("industry_policy") or {}
    if not availability.get("financial") and (
        value_quality.get("assessment") not in UNKNOWN_VALUES
        or financial_health.get("profit_trend") not in UNKNOWN_VALUES
    ):
        return "financial_opinion_requires_financial_data"
    if not availability.get("cashflow") and financial_health.get("cashflow_trend") not in UNKNOWN_VALUES:
        return "cashflow_opinion_requires_cashflow_data"
    if not availability.get("main_fund_flow") and market_flow.get("flow_health") not in UNKNOWN_VALUES:
        return "flow_opinion_requires_main_fund_flow_data"
    if not availability.get("main_fund_flow") and bool(market_flow.get("price_flow_divergence")):
        return "flow_divergence_requires_main_fund_flow_data"
    if industry_policy.get("policy_relevance") not in UNKNOWN_VALUES and not _has_policy_evidence(candidate):
        return "policy_opinion_requires_policy_evidence"
    return ""


def _has_policy_evidence(candidate: Dict[str, object]) -> bool:
    for item in candidate.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        searchable = " ".join(
            str(item.get(key) or "")
            for key in ("evidence_id", "type", "source_type", "title")
        )
        if "policy" in searchable.lower() or "政策" in searchable:
            return True
    return False
