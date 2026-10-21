from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from trader.infra.settings import DeepSeekSettings
from trader.recommendation.domain.evidence.review import (
    ReviewOutcome,
)
from trader.recommendation.domain.market.models import (
    Evidence,
    FeatureSnapshot,
    MarketQuote,
)
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.domain.risk.fusion import DIMENSION_NAMES
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.cache import ReviewCache
from trader.recommendation.infra.deepseek.client import DeepSeekHttpClient
from trader.recommendation.infra.deepseek.reviewer import DeepSeekReviewer
from trader.recommendation.infra.deepseek.schema import SCHEMA_VERSION
from trader.recommendation.infra.deepseek.schema_prompts import ReviewCacheKey

NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)


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
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=2)
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


def _reviewer(
    budget,
    *,
    post,
    hard_limit: int,
    cache: ReviewCache | None = None,
) -> DeepSeekReviewer:
    settings = DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=1.0,
        batch_size=8,
        max_tokens=256,
        daily_hard_limit=hard_limit,
        strategy_limits={
            "tomorrow": hard_limit,
            "d25": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_targets={"tomorrow_morning": 0},
        stage_limits={"tomorrow_morning": hard_limit},
        api_key="secret",
    )
    weights = {name: 0.2 for name in DIMENSION_NAMES}
    return DeepSeekReviewer(
        settings,
        budget,
        DeepSeekHttpClient(post=post, sleep=lambda _seconds: None),
        cache or ReviewCache(),
        dimension_weights={strategy: weights for strategy in Strategy},
        strategy_version="test-current",
        confidence_coverage_min=0.5,
        minimum_known_dimensions=2,
        now=lambda: NOW,
    )


def _budget(path, *, hard_limit: int) -> DeepSeekBudgetLedger:
    ledger = DeepSeekBudgetLedger(
        path,
        daily_hard_limit=hard_limit,
        strategy_limits={
            "tomorrow": hard_limit,
            "d25": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_targets={"tomorrow_morning": 0},
        stage_limits={"tomorrow_morning": hard_limit},
    )
    ledger.initialize()
    return ledger


def _candidate() -> FeatureSnapshot:
    quote = MarketQuote(
        code="600001",
        name="测试股份",
        price=12.0,
        previous_close=11.8,
        open_price=11.9,
        high=12.1,
        low=11.7,
        pct_change=1.0,
        change_5m=0.2,
        speed=0.1,
        volume_ratio=2.0,
        turnover_rate=3.0,
        amount=100_000_000.0,
        amplitude=3.0,
        market_cap=10_000_000_000.0,
        industry="软件",
        source="fixture",
        source_time=NOW,
        received_time=NOW,
        data_version="fixture",
        is_st=False,
        is_suspended=False,
        is_one_price_limit=False,
    )
    return FeatureSnapshot(
        quote=quote,
        values={"momentum": 60.0},
        observed_at=NOW,
        evidence=(Evidence("e-1", "news", "监管核验材料", "exchange", NOW, NOW, "evidence"),),
    )


def _primary_payload() -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "results": [
            {
                "code": "600001",
                "abstain": False,
                "catalyst": {
                    "direction": "positive",
                    "importance": "high",
                    "confirmation": "confirmed",
                    "cycle": "short",
                    "evidence_ids": ["e-1"],
                },
                "price_reaction": {"bucket": "not_reflected", "evidence_ids": ["e-1"]},
                "fundamental": {"direction": "improving", "evidence_ids": ["e-1"]},
                "industry_policy": {"direction": "positive", "evidence_ids": ["e-1"]},
                "risks": {
                    name: {
                        "present": False,
                        "severity": "low",
                        "confidence": 0.0,
                        "evidence_ids": [],
                        "assessment": "not present",
                    }
                    for name in (
                        "regulatory",
                        "shareholder_reduction",
                        "unlock",
                        "pledge",
                        "litigation",
                        "earnings",
                    )
                },
                "conflicts": [],
                "coverage": 0.8,
            }
        ],
    }


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload
