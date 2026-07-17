"""Pure, deterministic derivation of point-in-time news signals."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from trader.domain.models import Evidence


@dataclass(frozen=True)
class NewsSignalPolicy:
    lookback_hours: float
    freshness_full_score_hours: float
    positive_score: float
    neutral_score: float
    negative_score: float
    positive_keywords: tuple[str, ...]
    negative_keywords: tuple[str, ...]

    def __post_init__(self) -> None:
        numeric = (
            self.lookback_hours,
            self.freshness_full_score_hours,
            self.positive_score,
            self.neutral_score,
            self.negative_score,
        )
        if any(not math.isfinite(value) for value in numeric):
            raise ValueError("news signal policy values must be finite")
        if not 0.0 <= self.freshness_full_score_hours < self.lookback_hours:
            raise ValueError("news freshness hours must satisfy 0 <= full score < lookback")
        if not 0.0 <= self.negative_score < self.neutral_score < self.positive_score <= 100.0:
            raise ValueError("news signal scores must satisfy negative < neutral < positive")
        for name, keywords in (
            ("positive", self.positive_keywords),
            ("negative", self.negative_keywords),
        ):
            if not keywords or any(not keyword.strip() for keyword in keywords):
                raise ValueError(f"{name} news keywords must be non-empty")
            if len(keywords) != len(set(keywords)):
                raise ValueError(f"{name} news keywords must be unique")
        if set(self.positive_keywords) & set(self.negative_keywords):
            raise ValueError("positive and negative news keywords must not overlap")


@dataclass(frozen=True)
class NewsSignals:
    sentiment_score: float | None
    freshness_score: float | None


def derive_news_signals(
    evidence: Iterable[Evidence],
    *,
    observed_at: datetime,
    policy: NewsSignalPolicy,
) -> NewsSignals:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("news signal observation time must be timezone-aware")
    lookback_seconds = policy.lookback_hours * 3600.0
    full_score_seconds = policy.freshness_full_score_hours * 3600.0
    headline_scores: list[float] = []
    ages: list[float] = []
    seen_ids: set[str] = set()
    for item in evidence:
        if item.evidence_id in seen_ids or item.evidence_type != "news" or not item.title.strip():
            continue
        if item.published_at.tzinfo is None or item.published_at.utcoffset() is None:
            continue
        age_seconds = (observed_at - item.published_at).total_seconds()
        if age_seconds < 0.0 or age_seconds > lookback_seconds:
            continue
        seen_ids.add(item.evidence_id)
        headline_scores.append(_headline_score(item.title, policy))
        ages.append(age_seconds)
    if not headline_scores:
        return NewsSignals(None, None)
    newest_age = min(ages)
    if newest_age <= full_score_seconds:
        freshness = 100.0
    else:
        freshness = 100.0 * (lookback_seconds - newest_age) / (lookback_seconds - full_score_seconds)
    return NewsSignals(
        sentiment_score=sum(headline_scores) / len(headline_scores),
        freshness_score=freshness,
    )


def _headline_score(title: str, policy: NewsSignalPolicy) -> float:
    positive_hits = sum(keyword in title for keyword in policy.positive_keywords)
    negative_hits = sum(keyword in title for keyword in policy.negative_keywords)
    if positive_hits > negative_hits:
        return policy.positive_score
    if negative_hits > positive_hits:
        return policy.negative_score
    return policy.neutral_score


__all__ = ["NewsSignalPolicy", "NewsSignals", "derive_news_signals"]
