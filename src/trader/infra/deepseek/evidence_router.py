"""Deterministic point-in-time evidence selection for DeepSeek prompts."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta, timezone

from trader.domain.market.models import (
    Evidence,
    FeatureSnapshot,
)

MAX_PROMPT_EVIDENCE_PER_CANDIDATE = 12
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
    "official_media": 1,
    "eastmoney_announcement": 1,
    "eastmoney_financial": 1,
    "eastmoney_pledge": 1,
    "eastmoney_unlock": 1,
    "eastmoney_news": 2,
}
_OFFICIAL_SOURCES = frozenset({"exchange", "official_media"})
_TRUSTED_SOURCES = frozenset(
    {
        "exchange",
        "official_media",
        "eastmoney_announcement",
        "eastmoney_financial",
        "eastmoney_pledge",
        "eastmoney_unlock",
        "eastmoney_news",
    }
)


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

    selected: dict[str, list[Evidence]] = {category: [] for category in _CATEGORY_ORDER}
    remaining_capacity = MAX_PROMPT_EVIDENCE_PER_CANDIDATE
    for category in _CATEGORY_ORDER:
        initial = grouped[category][: min(_INITIAL_LIMITS[category], remaining_capacity)]
        selected[category].extend(initial)
        remaining_capacity -= len(initial)
        if remaining_capacity <= 0:
            break
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
    reasons = (
        _identity_invalid_reason(item),
        _time_invalid_reason(item, candidate, category),
        _content_invalid_reason(item),
    )
    return next((reason for reason in reasons if reason), "")


def _identity_invalid_reason(item: Evidence) -> str:
    if not item.evidence_id or len(item.evidence_id) > 80:
        return "invalid_evidence_id"
    if not item.data_version:
        return "missing_data_version"
    if item.received_at is None:
        return "missing_received_at"
    return ""


def _time_invalid_reason(item: Evidence, candidate: FeatureSnapshot, category: str) -> str:
    if item.received_at is None:
        return "missing_received_at"
    if (
        item.published_at.tzinfo is None
        or item.published_at.utcoffset() is None
        or item.received_at.tzinfo is None
        or item.received_at.utcoffset() is None
        or candidate.observed_at.tzinfo is None
        or candidate.observed_at.utcoffset() is None
    ):
        return "invalid_evidence_time"
    if item.published_at > candidate.observed_at or item.received_at > candidate.observed_at:
        return "future_evidence"
    if candidate.observed_at - item.published_at > _TTL_BY_CATEGORY[category]:
        return "expired_evidence"
    return ""


def _content_invalid_reason(item: Evidence) -> str:
    if not item.title or len(item.title) > 240:
        return "invalid_evidence_content"
    return ""


def _deduplicate(items: list[tuple[str, Evidence]]) -> tuple[tuple[str, Evidence], ...]:
    selected: dict[tuple[str, str], tuple[str, Evidence]] = {}
    for category, item in items:
        key = (category, event_key(item))
        current = selected.get(key)
        if current is None or _quality_key(item) < _quality_key(current[1]):
            selected[key] = (category, item)
    return tuple(selected.values())


def _quality_key(item: Evidence) -> tuple[int, float, str]:
    received_at = item.received_at.timestamp() if item.received_at is not None else float("inf")
    return (_SOURCE_QUALITY.get(item.source, 10), received_at, item.evidence_id)


def event_key(item: Evidence) -> str:
    normalized_title = " ".join(item.title.strip().lower().split())
    content_hash = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
    normalized_published_at = item.published_at.astimezone(timezone.utc).isoformat()
    return f"{normalized_published_at}:{content_hash}"


def source_tier(item: Evidence) -> str:
    if item.source in _OFFICIAL_SOURCES:
        return "official"
    if item.source in _TRUSTED_SOURCES:
        return "trusted"
    return "soft"


def evidence_quality(
    evidence: tuple[Evidence, ...], evidence_ids: tuple[str, ...], *, conflicted: bool = False
) -> float:
    if conflicted:
        return 0.25
    selected = [item for item in evidence if item.evidence_id in set(evidence_ids)]
    if any(source_tier(item) == "official" for item in selected):
        return 1.0
    trusted_sources = {item.source for item in selected if source_tier(item) == "trusted"}
    if len(trusted_sources) >= 2:
        return 0.85
    if len(trusted_sources) == 1:
        return 0.65
    return 0.4 if selected else 0.0


__all__ = [
    "MAX_PROMPT_EVIDENCE_PER_CANDIDATE",
    "RoutedEvidence",
    "event_key",
    "evidence_quality",
    "route_prompt_evidence",
    "source_tier",
]
