from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from trader.domain.market.models import Evidence, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import ReviewOutcome
from trader.infra.deepseek.budget import DeepSeekBudgetStore
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.deepseek.client import DeepSeekHttpClient
from trader.infra.deepseek.evidence_router import route_prompt_evidence
from trader.infra.deepseek.reviewer import DeepSeekReviewer
from trader.infra.deepseek.schema import DeepSeekSchemaError, build_messages, parse_reviews
from trader.infra.settings import DeepSeekSettings

NOW = datetime(2026, 7, 16, 6, 30, tzinfo=timezone.utc)


def test_long_review_is_permanently_empty_and_does_not_create_budget_rows(tmp_path) -> None:
    calls = 0

    def post(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise AssertionError("long must never call DeepSeek")

    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=188)
    reviewer = _reviewer(budget, post=post)

    result = reviewer.review(
        Strategy.LONG,
        (_candidate(),),
        phase="afternoon",
        deadline=NOW + timedelta(minutes=1),
    )

    assert result == {}
    assert calls == 0
    assert budget.summary(NOW.date().isoformat())["used"] == 0
    assert reviewer.status()["last_strategy"] == "long"
    assert reviewer.status()["last_candidate_count"] == 0


@pytest.mark.parametrize("emergency", [False, True])
def test_budget_rejects_all_long_reservations(tmp_path, emergency: bool) -> None:
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=188)

    result = budget.reserve(
        Strategy.LONG,
        phase="afternoon",
        requested_at=NOW,
        emergency=emergency,
        emergency_reason="new_high_risk" if emergency else "",
    )

    assert (result.allowed, result.reason) == (False, "long_not_allowed")
    assert budget.summary(NOW.date().isoformat())["used"] == 0


def test_budget_rejects_long_bucket_even_for_non_long_strategy(tmp_path) -> None:
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=188)

    result = budget.reserve(
        Strategy.TODAY,
        phase="shared_preheat",
        requested_at=NOW,
        bucket="long",
    )

    assert (result.allowed, result.reason) == (False, "long_not_allowed")
    assert budget.summary(NOW.date().isoformat())["used"] == 0


def test_v4_facts_reject_forbidden_model_decision_fields() -> None:
    candidate = _candidate()
    payload = _v4_payload(candidate.quote.code)
    payload["results"][0]["final_score"] = 99

    with pytest.raises(DeepSeekSchemaError, match="forbidden model decision field"):
        parse_reviews(json.dumps(payload), [candidate], NOW)


def test_v4_facts_reject_unsupported_schema_version() -> None:
    candidate = _candidate()
    payload = _v4_payload(candidate.quote.code)
    payload["schema_version"] = "unexpected"

    with pytest.raises(DeepSeekSchemaError, match="unsupported schema_version"):
        parse_reviews(json.dumps(payload), [candidate], NOW)


def test_v4_facts_validate_catalyst_confirmation_and_cycle() -> None:
    candidate = _candidate()
    payload = _v4_payload(candidate.quote.code)
    payload["results"][0]["catalyst"]["confirmation"] = "maybe"

    with pytest.raises(DeepSeekSchemaError, match="catalyst.confirmation"):
        parse_reviews(json.dumps(payload), [candidate], NOW)


def test_v4_positive_soft_news_does_not_add_score_without_two_trusted_sources() -> None:
    soft = _candidate(
        evidence=(Evidence("soft-1", "news", "市场传闻订单增长", "social_media", NOW - timedelta(hours=1), NOW, "v1"),)
    )
    payload = _v4_payload(soft.quote.code, evidence_ids=("soft-1",))

    review = parse_reviews(json.dumps(payload), [soft], NOW)[soft.quote.code]

    assert review.outcome is ReviewOutcome.ABSTAIN
    assert review.dimensions["value_quality"].score == 50.0
    assert review.dimensions["value_quality"].confidence == 0.4


def test_v4_positive_fact_uses_two_independent_trusted_sources() -> None:
    trusted = _candidate(
        evidence=(
            Evidence("trusted-1", "news", "公司确认订单增长", "eastmoney_news", NOW - timedelta(hours=1), NOW, "v1"),
            Evidence(
                "trusted-2", "news", "行业协会确认订单增长", "official_media", NOW - timedelta(hours=1), NOW, "v2"
            ),
            Evidence(
                "official-1",
                "announcement",
                "交易所公告业绩改善",
                "exchange",
                NOW - timedelta(hours=1),
                NOW,
                "v3",
            ),
        )
    )
    payload = _v4_payload(trusted.quote.code, evidence_ids=("trusted-1", "trusted-2", "official-1"))

    review = parse_reviews(json.dumps(payload), [trusted], NOW)[trusted.quote.code]

    assert review.outcome is ReviewOutcome.APPLIED
    assert review.dimensions["value_quality"].score == 65.0
    assert review.dimensions["value_quality"].confidence == 1.0
    assert review.raw_confidence is not None and review.raw_confidence >= 0.6


def test_v4_risk_facts_do_not_directly_change_dimension_score() -> None:
    candidate = _candidate()
    payload = _v4_payload(candidate.quote.code)
    payload["results"][0]["risks"]["regulatory"] = {
        "present": True,
        "severity": "high",
        "confidence": 0.9,
        "evidence_ids": ["e-1"],
        "assessment": "regulatory risk",
    }

    review = parse_reviews(json.dumps(payload), [candidate], NOW)[candidate.quote.code]

    assert review.risk_facts[0].risk_code == "regulatory_risk"
    assert review.risk_facts[0].penalty == 0.0
    assert review.risk_facts[0].veto is False
    assert review.dimensions["risk_quality"].score == 50.0


def test_v4_prompt_requests_facts_not_scores_actions_targets_or_rankings() -> None:
    prompt = build_messages([_candidate()])[1]["content"]

    assert "deepseek_v4_review_facts_v1" in prompt
    assert "催化方向" in prompt
    assert "不得输出目标价、最终分、排名、动作或生产扣分" in prompt
    assert "dimensions" not in prompt.partition("动态候选JSON=")[0]


def test_prompt_evidence_router_caps_each_stock_at_twelve_items_and_dedupes_events() -> None:
    candidate = _candidate(
        evidence=tuple(
            Evidence(
                f"news-{index:02d}",
                "news",
                "同一事件标题" if index < 3 else f"news {index}",
                "eastmoney_news",
                NOW - timedelta(hours=1),
                NOW,
                f"v{index}",
            )
            for index in range(15)
        )
    )

    routed = route_prompt_evidence(candidate)

    assert len(routed.evidence) <= 12
    assert sum(item.title == "同一事件标题" for item in routed.evidence) == 1


def test_budget_enforces_c2_soft_bucket_limits(tmp_path) -> None:
    budget = _budget(tmp_path / "runtime.sqlite3", hard_limit=188)

    reservations = [budget.reserve(Strategy.TODAY, phase="today_main", requested_at=NOW) for _ in range(23)]

    assert sum(item.allowed for item in reservations) == 22
    assert (reservations[-1].allowed, reservations[-1].reason) == (False, "soft_bucket_limit")


def _reviewer(budget: DeepSeekBudgetStore, *, post) -> DeepSeekReviewer:
    weights = {
        "value_quality": 0.2,
        "financial_health": 0.2,
        "market_flow": 0.2,
        "industry_policy": 0.2,
        "risk_quality": 0.2,
    }
    return DeepSeekReviewer(
        _settings(),
        budget,
        DeepSeekHttpClient(post=post, sleep=lambda _seconds: None),
        cache=ReviewCache(),
        dimension_weights={strategy: weights for strategy in Strategy},
        strategy_version="c2-test",
        confidence_coverage_min=0.5,
        minimum_known_dimensions=2,
        now=lambda: NOW,
    )


def _settings() -> DeepSeekSettings:
    return DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.example/v1",
        model="deepseek-v4-flash",
        challenger_model="deepseek-v4-pro",
        challenger_limits={"today": 6, "tomorrow": 6, "d25": 5, "long": 0},
        timeout_seconds=1.0,
        batch_size=8,
        max_tokens=256,
        daily_hard_limit=188,
        strategy_limits={"today": 70, "tomorrow": 45, "d25": 35, "long": 18, "shared_preheat": 15, "emergency": 5},
        stage_targets={
            "today_main": 0,
            "tomorrow_afternoon": 0,
            "d25_afternoon": 0,
            "long_afternoon": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_limits={
            "today_main": 70,
            "tomorrow_afternoon": 45,
            "d25_afternoon": 35,
            "long_afternoon": 18,
            "shared_preheat": 15,
            "emergency": 5,
        },
        api_key="secret",
    )


def _budget(path, *, hard_limit: int) -> DeepSeekBudgetStore:
    budget = DeepSeekBudgetStore(
        path,
        daily_hard_limit=hard_limit,
        strategy_limits={"today": 70, "tomorrow": 45, "d25": 35, "long": 18, "shared_preheat": 15, "emergency": 5},
        stage_targets={
            "today_main": 0,
            "tomorrow_afternoon": 0,
            "d25_afternoon": 0,
            "long_afternoon": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_limits={
            "today_main": 70,
            "tomorrow_afternoon": 45,
            "d25_afternoon": 35,
            "long_afternoon": 18,
            "shared_preheat": 15,
            "emergency": 5,
        },
        challenger_limits={"today": 6, "tomorrow": 6, "d25": 5, "long": 0},
    )
    budget.initialize()
    return budget


def _candidate(*, evidence: tuple[Evidence, ...] | None = None) -> FeatureSnapshot:
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
    )
    return FeatureSnapshot(
        quote=quote,
        values={"relative_strength_5d": 65.0, "industry_policy_score": 60.0, "value_score": 55.0},
        observed_at=NOW,
        history_days=60,
        evidence=evidence
        if evidence is not None
        else (Evidence("e-1", "announcement", "交易所公告业绩改善", "exchange", NOW - timedelta(hours=1), NOW, "v1"),),
    )


def _v4_payload(code: str, *, evidence_ids: tuple[str, ...] = ("e-1",)) -> dict[str, object]:
    return {
        "schema_version": "deepseek_v4_review_facts_v1",
        "results": [
            {
                "code": code,
                "abstain": False,
                "catalyst": {
                    "direction": "positive",
                    "importance": "high",
                    "confirmation": "confirmed",
                    "cycle": "short",
                    "evidence_ids": list(evidence_ids),
                },
                "price_reaction": {"bucket": "not_reflected", "evidence_ids": list(evidence_ids)},
                "fundamental": {"direction": "improving", "evidence_ids": list(evidence_ids)},
                "industry_policy": {"direction": "positive", "evidence_ids": list(evidence_ids)},
                "risks": {
                    "regulatory": {"present": False, "severity": "low", "confidence": 0.0, "evidence_ids": []},
                    "shareholder_reduction": {
                        "present": False,
                        "severity": "low",
                        "confidence": 0.0,
                        "evidence_ids": [],
                    },
                    "unlock": {"present": False, "severity": "low", "confidence": 0.0, "evidence_ids": []},
                    "pledge": {"present": False, "severity": "low", "confidence": 0.0, "evidence_ids": []},
                    "litigation": {"present": False, "severity": "low", "confidence": 0.0, "evidence_ids": []},
                    "earnings": {"present": False, "severity": "low", "confidence": 0.0, "evidence_ids": []},
                },
                "conflicts": [],
                "coverage": 0.85,
            }
        ],
    }
