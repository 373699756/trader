from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from trader.domain.market.models import (
    Evidence,
    FeatureSnapshot,
    MarketQuote,
)
from trader.domain.recommendation.fusion import DIMENSION_NAMES
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import (
    ReviewCandidateContext,
    ReviewOutcome,
)
from trader.infra.deepseek.budget import DeepSeekBudgetStore
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.deepseek.challenger import parse_challenger_reviews
from trader.infra.deepseek.client import DeepSeekHttpClient
from trader.infra.deepseek.reviewer import DeepSeekReviewer
from trader.infra.settings import DeepSeekSettings

NOW = datetime(2026, 7, 16, 2, 0, tzinfo=timezone.utc)


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
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=2, challenger_limit=0)
    reviewer = _reviewer(
        budget,
        post=lambda *_args, **_kwargs: next(responses),
        hard_limit=2,
        challenger_limit=0,
    )

    result = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert result[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert budget.summary(NOW.date().isoformat())["used"] == 2


def test_reviewer_runs_bounded_challenger_and_persists_model_audit(tmp_path) -> None:
    candidate = _candidate()
    database_path = tmp_path / "runtime.sqlite3"
    budget = _budget(database_path, hard_limit=2, challenger_limit=1)
    requested_models: list[str] = []

    def post(*_args, **kwargs):
        model = str(kwargs["json"]["model"])
        requested_models.append(model)
        payload = _challenger_payload() if model == "deepseek-v4-pro" else _primary_payload()
        return _Response(
            {
                "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
                "model": f"{model}-202607",
                "system_fingerprint": f"fp-{model}",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "prompt_cache_hit_tokens": 3,
                    "prompt_cache_miss_tokens": 7,
                },
            }
        )

    reviewer = _reviewer(budget, post=post, hard_limit=2, challenger_limit=1)
    result = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
        contexts={candidate.quote.code: ReviewCandidateContext(70.0, 1, 70.0, True)},
    )

    review = result[candidate.quote.code]
    assert requested_models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert review.review_stage == "primary+challenger"
    assert review.challenger_status == "applied"
    assert review.actual_model == "deepseek-v4-flash-202607"
    assert review.challenger_actual_model == "deepseek-v4-pro-202607"
    assert review.dimensions["market_flow"].confidence == 0.6
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT model_role, requested_model, actual_model, reasoning_effort, total_tokens "
            "FROM deepseek_calls ORDER BY rowid"
        ).fetchall()
    assert rows == [
        ("primary", "deepseek-v4-flash", "deepseek-v4-flash-202607", None, 15),
        ("challenger", "deepseek-v4-pro", "deepseek-v4-pro-202607", "high", 15),
    ]


def test_reviewer_reuses_strategy_scoped_challenger_cache_without_physical_calls(tmp_path) -> None:
    candidate = _candidate()
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=4, challenger_limit=2)
    cache = ReviewCache()
    requested_models: list[str] = []

    def post(*_args, **kwargs):
        model = str(kwargs["json"]["model"])
        requested_models.append(model)
        payload = _challenger_payload() if model == "deepseek-v4-pro" else _primary_payload()
        return _Response(
            {
                "choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}],
                "model": f"{model}-202607",
            }
        )

    reviewer = _reviewer(budget, post=post, hard_limit=4, challenger_limit=2, cache=cache)
    context = {candidate.quote.code: ReviewCandidateContext(70.0, 1, 70.0, True)}

    first = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
        contexts=context,
    )
    second = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
        contexts=context,
    )

    assert requested_models == ["deepseek-v4-flash", "deepseek-v4-pro"]
    assert budget.summary(NOW.date().isoformat())["used"] == 2
    assert first[candidate.quote.code] == second[candidate.quote.code]
    assert second[candidate.quote.code].challenger_status == "applied"


def test_challenger_schema_repair_round_trips_transient_reasoning_content(tmp_path) -> None:
    candidate = _candidate()
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=3, challenger_limit=2)
    challenger_payloads: list[dict[str, object]] = []

    def post(*_args, **kwargs):
        payload = kwargs["json"]
        model = str(payload["model"])
        if model == "deepseek-v4-flash":
            return _Response({"choices": [{"message": {"content": json.dumps(_primary_payload())}}]})
        challenger_payloads.append(payload)
        if len(challenger_payloads) == 1:
            return _Response(
                {
                    "choices": [
                        {
                            "message": {"content": "{}", "reasoning_content": "transient-reasoning"},
                            "finish_reason": "stop",
                        }
                    ]
                }
            )
        return _Response(
            {"choices": [{"message": {"content": json.dumps(_challenger_payload())}, "finish_reason": "stop"}]}
        )

    reviewer = _reviewer(budget, post=post, hard_limit=3, challenger_limit=2)
    result = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
        contexts={candidate.quote.code: ReviewCandidateContext(70.0, 1, 70.0, True)},
    )

    repair_messages = challenger_payloads[1]["messages"]
    assert isinstance(repair_messages, list)
    assert repair_messages[-2]["reasoning_content"] == "transient-reasoning"
    assert result[candidate.quote.code].challenger_status == "applied"


def test_challenger_schema_failure_keeps_valid_primary_review(tmp_path) -> None:
    candidate = _candidate()
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=3, challenger_limit=2)

    def post(*_args, **kwargs):
        model = str(kwargs["json"]["model"])
        content = json.dumps(_primary_payload()) if model == "deepseek-v4-flash" else "{}"
        return _Response({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})

    reviewer = _reviewer(budget, post=post, hard_limit=3, challenger_limit=2)
    result = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
        contexts={candidate.quote.code: ReviewCandidateContext(70.0, 1, 70.0, True)},
    )

    review = result[candidate.quote.code]
    assert review.outcome is ReviewOutcome.APPLIED
    assert review.review_stage == "primary"
    assert review.challenger_status == "schema_invalid"
    assert budget.summary(NOW.date().isoformat())["used"] == 3


@pytest.mark.parametrize(
    ("target", "field"),
    (
        ("result", "action"),
        ("dimensions", "final_score"),
        ("dimension", "score"),
    ),
)
def test_challenger_schema_rejects_unknown_decision_fields(target: str, field: str) -> None:
    candidate = _candidate()
    payload = _challenger_payload()
    result = payload["results"][0]
    if target == "result":
        result[field] = "buy"
    elif target == "dimensions":
        result["dimensions"][field] = 99
    else:
        result["dimensions"]["market_flow"][field] = 99

    with pytest.raises(ValueError, match="unknown challenger"):
        parse_challenger_reviews(json.dumps(payload), (candidate,), NOW)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    (
        ("code", 600001, "code must be a string"),
        ("reason_code", 123, "reason_code"),
    ),
)
def test_challenger_schema_rejects_non_string_text_fields(field: str, value: object, error: str) -> None:
    candidate = _candidate()
    payload = _challenger_payload()
    result = payload["results"][0]
    if field == "code":
        result[field] = value
    else:
        result["dimensions"]["market_flow"][field] = value

    with pytest.raises(ValueError, match=error):
        parse_challenger_reviews(json.dumps(payload), (candidate,), NOW)


def test_budget_enforces_challenger_limit_inside_strategy_bucket(tmp_path) -> None:
    store = _budget(tmp_path / "runtime.sqlite3", hard_limit=2, challenger_limit=1)

    first = store.reserve(
        Strategy.TODAY,
        phase="today_main",
        requested_at=NOW,
        model_role="challenger",
        requested_model="deepseek-v4-pro",
        reasoning_effort="high",
    )
    second = store.reserve(
        Strategy.TODAY,
        phase="today_main",
        requested_at=NOW,
        model_role="challenger",
        requested_model="deepseek-v4-pro",
        reasoning_effort="high",
    )

    assert first.allowed is True
    assert (second.allowed, second.reason) == (False, "challenger_limit")
    assert store.summary(NOW.date().isoformat())["by_model_role"] == {"primary": 0, "challenger": 1}


def _reviewer(
    budget,
    *,
    post,
    hard_limit: int,
    challenger_limit: int,
    cache: ReviewCache | None = None,
) -> DeepSeekReviewer:
    settings = DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        challenger_model="deepseek-v4-pro",
        challenger_limits={"today": challenger_limit, "tomorrow": 0, "d25": 0, "long": 0},
        timeout_seconds=1.0,
        batch_size=8,
        max_tokens=256,
        daily_hard_limit=hard_limit,
        strategy_limits={
            "today": hard_limit,
            "tomorrow": 0,
            "d25": 0,
            "long": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_targets={"today_main": 0},
        stage_limits={"today_main": hard_limit},
        api_key="secret",
    )
    weights = {name: 0.2 for name in DIMENSION_NAMES}
    return DeepSeekReviewer(
        settings,
        budget,
        DeepSeekHttpClient(post=post, sleep=lambda _seconds: None),
        cache or ReviewCache(),
        dimension_weights={strategy: weights for strategy in Strategy},
        strategy_version="test-v4",
        confidence_coverage_min=0.5,
        minimum_known_dimensions=2,
        now=lambda: NOW,
    )


def _budget(path, *, hard_limit: int, challenger_limit: int) -> DeepSeekBudgetStore:
    store = DeepSeekBudgetStore(
        path,
        daily_hard_limit=hard_limit,
        strategy_limits={
            "today": hard_limit,
            "tomorrow": 0,
            "d25": 0,
            "long": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_targets={"today_main": 0},
        stage_limits={"today_main": hard_limit},
        challenger_limits={"today": challenger_limit, "tomorrow": 0, "d25": 0, "long": 0},
    )
    store.initialize()
    return store


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
        data_version="fixture-v1",
        is_st=False,
        is_suspended=False,
        is_one_price_limit=False,
    )
    return FeatureSnapshot(
        quote=quote,
        values={"momentum": 60.0},
        observed_at=NOW,
        evidence=(Evidence("e-1", "news", "监管核验材料", "exchange", NOW, NOW, "evidence-v1"),),
    )


def _primary_payload() -> dict[str, object]:
    dimensions = {
        name: {
            "score": 80,
            "confidence": 0.8,
            "raw_confidence": 0.8,
            "assessment": "positive",
            "flags": [],
            "evidence_ids": ["e-1"],
            "unknown": False,
        }
        for name in DIMENSION_NAMES
    }
    return {"results": [{"code": "600001", "abstain": False, "dimensions": dimensions, "risk_facts": []}]}


def _challenger_payload() -> dict[str, object]:
    dimensions = {
        name: {
            "verdict": "confirm",
            "raw_confidence": 0.6,
            "evidence_ids": ["e-1"],
            "reason_code": "supported",
        }
        for name in DIMENSION_NAMES
    }
    return {"results": [{"code": "600001", "dimensions": dimensions}]}


class _Response:
    status_code = 200
    headers: dict[str, str] = {}

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload
