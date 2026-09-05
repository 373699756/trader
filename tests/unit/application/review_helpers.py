"""Shared typed review fixtures for scored projection tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from trader.domain.review.models import DeepSeekReview, DimensionAssessment, ReviewOutcome

NOW = datetime(2026, 7, 28, 14, 40, tzinfo=ZoneInfo("Asia/Shanghai"))


def review(code: str, score: float) -> DeepSeekReview:
    dimensions = {
        name: DimensionAssessment(
            name=name,
            score=score,
            confidence=1.0 if name != "industry_policy" else 0.0,
            assessment="fixture",
            evidence_ids=("evidence-1",) if name != "industry_policy" else (),
            is_unknown=name == "industry_policy",
        )
        for name in (
            "value_quality",
            "financial_health",
            "market_flow",
            "industry_policy",
            "risk_quality",
        )
    }
    return DeepSeekReview(
        code=code,
        outcome=ReviewOutcome.APPLIED,
        dimensions=dimensions,
        risk_facts=(),
        completed_at=NOW,
        evidence_manifest_hash=f"manifest:{code}",
    )
