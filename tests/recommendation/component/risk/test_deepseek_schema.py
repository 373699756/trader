from __future__ import annotations

import json

from dataclasses import replace

from datetime import timedelta


from trader.recommendation.domain.evidence.review import (
    ReviewOutcome,
)


from trader.recommendation.domain.publication.models import Strategy









from trader.recommendation.infra.deepseek.schema_prompts import ReviewCacheKey

from tests.helpers.recommendation.risk.deepseek_schema import (
    NOW,
    _Response,
    _budget,
    _candidate,
    _primary_payload,
    _reviewer,
)


def test_review_cache_reuses_same_facts_after_delivery_refresh() -> None:
    candidate = _candidate()
    later = NOW + timedelta(seconds=30)
    refreshed = replace(
        candidate,
        observed_at=later,
        quote=replace(candidate.quote, received_time=later, price=12.1),
        evidence=tuple(replace(item, received_at=later) for item in candidate.evidence),
    )
    assert ReviewCacheKey(candidate, model="test") == ReviewCacheKey(refreshed, model="test")
    revised = replace(refreshed, evidence=tuple(replace(item, title="新监管事实") for item in refreshed.evidence))
    assert ReviewCacheKey(candidate, model="test") != ReviewCacheKey(revised, model="test")
    next_day = replace(refreshed, observed_at=later + timedelta(days=1))
    assert ReviewCacheKey(candidate, model="test") != ReviewCacheKey(next_day, model="test")


def test_finish_reason_length_uses_the_single_schema_repair_attempt(tmp_path) -> None:
    candidate = _candidate()
    responses = iter(
        (
            _Response(
                {
                    "choices": [{"message": {"content": '{"results":['}, "finish_reason": "length"}],
                    "model": "deepseek-v4-flash-202607",
                }
            ),
            _Response(
                {
                    "choices": [{"message": {"content": json.dumps(_primary_payload())}, "finish_reason": "stop"}],
                    "model": "deepseek-v4-flash-202607",
                }
            ),
        )
    )
    budget = _budget(tmp_path / "state.json", hard_limit=2)
    reviewer = _reviewer(
        budget,
        post=lambda *_args, **_kwargs: next(responses),
        hard_limit=2,
    )

    result = reviewer.review(
        Strategy.TOMORROW,
        (candidate,),
        phase="morning_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert result[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert budget.summary(NOW.date().isoformat())["used"] == 2
