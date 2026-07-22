"""Deterministic point-in-time evidence selection for DeepSeek prompts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from trader.domain.market.models import (
    Evidence,
    FeatureSnapshot,
)

_INITIAL_LIMITS = {"market": 1, "tail": 1, "research": 3, "risk": 4, "news": 7}
_MAXIMUM_LIMITS = {"market": 1, "tail": 1, "research": 5, "risk": 6, "news": 8}
_FILL_ORDER = ("risk", "research", "tail", "news", "market")
_CATEGORY_ORDER = ("market", "tail", "research", "risk", "news")
_TYPE_CATEGORY = {
    "structured_point_in_time": "market",
    "intraday_tail": "tail",
    "financial_snapshot": "research",
    "financial_report": "research",
    "research_summary": "research",
    "regulatory_filing": "risk",
    "ownership_filing": "risk",
    "announcement": "news",
    "news": "news",
}
_TTL_BY_CATEGORY = {
    "market": timedelta(hours=1),
    "tail": timedelta(hours=4),
    "research": timedelta(days=550),
    "risk": timedelta(days=180),
    "news": timedelta(hours=72),
}
_SOURCE_QUALITY = {
    "exchange": 0,
    "eastmoney_announcement": 1,
    "eastmoney_financial": 1,
    "eastmoney_pledge": 1,
    "eastmoney_unlock": 1,
    "eastmoney_news": 2,
}


@dataclass(frozen=True)
class RoutedEvidence:
    evidence: tuple[Evidence, ...]
    exclusion_reasons: tuple[str, ...]


def route_prompt_evidence(candidate: FeatureSnapshot) -> RoutedEvidence:
    valid: list[tuple[str, Evidence]] = []
    reasons: list[str] = []
    for item in candidate.evidence:
        category = _TYPE_CATEGORY.get(item.evidence_type)
        reason = _invalid_reason(item, candidate, category)
        if reason:
            reasons.append(reason)
            continue
        if category is None:
            continue
        valid.append((category, item))

    deduplicated = _deduplicate(valid)
    grouped: dict[str, list[Evidence]] = defaultdict(list)
    for category, item in deduplicated:
        grouped[category].append(item)
    for items in grouped.values():
        items.sort(key=_quality_key)

    selected: dict[str, list[Evidence]] = {
        category: grouped[category][: _INITIAL_LIMITS[category]] for category in _CATEGORY_ORDER
    }
    remaining_capacity = 16 - sum(len(items) for items in selected.values())
    for category in _FILL_ORDER:
        if remaining_capacity <= 0:
            break
        current = selected[category]
        maximum = _MAXIMUM_LIMITS[category]
        available = grouped[category][len(current) : maximum]
        extra = available[:remaining_capacity]
        current.extend(extra)
        remaining_capacity -= len(extra)

    routed = tuple(sorted((item for items in selected.values() for item in items), key=lambda item: item.evidence_id))
    return RoutedEvidence(routed, tuple(dict.fromkeys(reasons)))


def _invalid_reason(item: Evidence, candidate: FeatureSnapshot, category: str | None) -> str:
    if category is None:
        return "unsupported_evidence_type"
    if not item.data_version:
        return "missing_data_version"
    if item.received_at is None:
        return "missing_received_at"
    if item.published_at.tzinfo is None or item.received_at.tzinfo is None:
        return "invalid_evidence_time"
    if item.published_at > candidate.observed_at or item.received_at > candidate.observed_at:
        return "future_evidence"
    if candidate.observed_at - item.published_at > _TTL_BY_CATEGORY[category]:
        return "expired_evidence"
    if not item.title or len(item.title) > 240:
        return "invalid_evidence_content"
    return ""


def _deduplicate(items: list[tuple[str, Evidence]]) -> tuple[tuple[str, Evidence], ...]:
    selected: dict[tuple[str, str, str], tuple[str, Evidence]] = {}
    for category, item in items:
        content_hash = hashlib.sha256(item.title.strip().encode("utf-8")).hexdigest()
        key = (item.evidence_type, item.published_at.isoformat(), content_hash)
        current = selected.get(key)
        if current is None or _quality_key(item) < _quality_key(current[1]):
            selected[key] = (category, item)
    return tuple(selected.values())


def _quality_key(item: Evidence) -> tuple[int, str, str]:
    received_at = item.received_at.isoformat() if item.received_at is not None else ""
    return (_SOURCE_QUALITY.get(item.source, 10), received_at, item.evidence_id)


__all__ = ["RoutedEvidence", "route_prompt_evidence"]
