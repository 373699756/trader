from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trader.domain.market.models import Evidence, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.models import Strategy
from trader.domain.review.models import ReviewOutcome
from trader.infra.deepseek.budget import DeepSeekBudgetStore
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.deepseek.client import DeepSeekHttpClient
from trader.infra.deepseek.reviewer import DeepSeekReviewer
from trader.infra.deepseek.schema import build_messages, review_cache_key
from trader.infra.settings import DeepSeekSettings

NOW = datetime(2026, 7, 16, 6, 30, tzinfo=timezone.utc)


class _Response:
    def __init__(self, payload: dict[str, object], status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")


def test_c3_same_stock_three_strategies_share_one_raw_facts_request(tmp_path: Path) -> None:
    calls: list[str] = []

    def post(*_args: Any, **kwargs: Any) -> _Response:
        calls.append(str(kwargs["json"]["model"]))
        return _ok_response(_candidate().quote.code)

    reviewer, budget = _reviewer(tmp_path / "runtime.sqlite3", post=post)
    candidate = _candidate()
    today_candidate = replace(
        candidate,
        board_policy_id="today-main",
        values={**candidate.values, "board_candidate_score": 81.0},
    )
    tomorrow_candidate = replace(
        candidate,
        board_policy_id="tomorrow-main",
        values={**candidate.values, "board_candidate_score": 72.0},
    )
    d25_candidate = replace(
        candidate,
        board_policy_id="d25-main",
        values={**candidate.values, "board_candidate_score": 63.0},
    )

    today = reviewer.review(
        Strategy.TODAY,
        (today_candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )
    tomorrow = reviewer.review(
        Strategy.TOMORROW,
        (tomorrow_candidate,),
        phase="afternoon",
        deadline=NOW + timedelta(minutes=1),
    )
    d25 = reviewer.review(
        Strategy.D25,
        (d25_candidate,),
        phase="afternoon",
        deadline=NOW + timedelta(minutes=1),
    )

    assert today[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert tomorrow[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert d25[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert calls == ["deepseek-v4-flash"]
    assert budget.summary(NOW.date().isoformat())["used"] == 1
    cache_status = reviewer.status().get("cache")
    assert isinstance(cache_status, Mapping)
    raw_hits = cache_status.get("raw_hits")
    assert isinstance(raw_hits, int)
    assert raw_hits >= 2


def test_c3_quote_only_cache_hit_adds_no_http_but_manifest_change_routes(tmp_path: Path) -> None:
    calls: list[str] = []

    def post(*_args: Any, **kwargs: Any) -> _Response:
        calls.append(str(kwargs["json"]["model"]))
        return _ok_response(_candidate().quote.code)

    reviewer, budget = _reviewer(tmp_path / "runtime.sqlite3", post=post)
    candidate = replace(_candidate(), merge_epoch="epoch-1")
    quote_only = replace(
        candidate,
        quote=replace(candidate.quote, price=12.01, data_version="quote-v2"),
        merge_epoch="epoch-2",
    )
    manifest_changed = _candidate(evidence_title="交易所公告新增合同")

    first = reviewer.review(Strategy.TODAY, (candidate,), phase="today_main", deadline=NOW + timedelta(minutes=1))
    second = reviewer.review(Strategy.TODAY, (quote_only,), phase="today_main", deadline=NOW + timedelta(minutes=1))
    third = reviewer.review(
        Strategy.TODAY,
        (manifest_changed,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert first[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert second[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert third[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert calls == ["deepseek-v4-flash", "deepseek-v4-flash"]
    assert budget.summary(NOW.date().isoformat())["used"] == 2


def test_c3_raw_cache_identity_covers_every_feature_rendered_in_prompt() -> None:
    candidate = _candidate()
    changed = replace(
        candidate,
        values={**candidate.values, "industry_policy_score": 61.0},
    )

    assert build_messages((candidate,)) != build_messages((changed,))
    assert review_cache_key(candidate, model="deepseek-v4-flash") != review_cache_key(
        changed,
        model="deepseek-v4-flash",
    )


def test_c3_strategy_only_scores_are_excluded_from_raw_facts_prompt() -> None:
    candidate = _candidate()
    changed = replace(
        candidate,
        board_policy_id="tomorrow-main",
        values={
            **candidate.values,
            "relative_strength_5d": 66.0,
            "board_candidate_score": 72.0,
        },
    )

    assert build_messages((candidate,)) == build_messages((changed,))
    assert review_cache_key(candidate, model="deepseek-v4-flash") == review_cache_key(
        changed,
        model="deepseek-v4-flash",
    )


def test_c3_raw_cache_identity_includes_evidence_receipt_time() -> None:
    candidate = _candidate()
    changed = replace(
        candidate,
        evidence=(
            replace(
                candidate.evidence[0],
                received_at=NOW - timedelta(minutes=1),
            ),
        ),
    )

    assert build_messages((candidate,)) != build_messages((changed,))
    assert review_cache_key(candidate, model="deepseek-v4-flash") != review_cache_key(
        changed,
        model="deepseek-v4-flash",
    )


def test_c3_long_late_hard_limit_and_all_fail_remain_degraded_not_blocking(tmp_path: Path) -> None:
    candidate = _candidate()
    calls = 0

    def post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _ok_response(candidate.quote.code)

    reviewer, budget = _reviewer(tmp_path / "runtime.sqlite3", post=post, hard_limit=1)
    long = reviewer.review(Strategy.LONG, (candidate,), phase="afternoon", deadline=NOW + timedelta(minutes=1))
    first = reviewer.review(Strategy.TODAY, (candidate,), phase="today_main", deadline=NOW + timedelta(minutes=1))
    second = reviewer.review(
        Strategy.TOMORROW,
        (_candidate(code="600002"),),
        phase="afternoon",
        deadline=NOW + timedelta(minutes=1),
    )

    assert long == {}
    assert first[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert second["600002"].outcome is ReviewOutcome.REJECTED
    assert second["600002"].error == "budget_exhausted"
    assert calls == 1
    assert budget.summary(NOW.date().isoformat())["used"] == 1

    late_reviewer, late_budget = _reviewer(
        tmp_path / "late.sqlite3",
        post=lambda *_args, **_kwargs: _ok_response(candidate.quote.code),
        now=lambda: NOW + timedelta(minutes=2),
    )
    late = late_reviewer.review(Strategy.TODAY, (candidate,), phase="today_main", deadline=NOW + timedelta(minutes=1))

    assert late[candidate.quote.code].outcome is ReviewOutcome.LATE
    assert late[candidate.quote.code].error == "deadline_reached"
    assert late_budget.summary(NOW.date().isoformat())["used"] == 0

    failed_calls = 0

    def fail_post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal failed_calls
        failed_calls += 1
        return _Response({}, status_code=500)

    failed_reviewer, failed_budget = _reviewer(tmp_path / "failed.sqlite3", post=fail_post, hard_limit=4)
    failed = failed_reviewer.review(
        Strategy.TODAY, (candidate,), phase="today_main", deadline=NOW + timedelta(minutes=1)
    )

    assert failed[candidate.quote.code].outcome is ReviewOutcome.REJECTED
    assert failed[candidate.quote.code].error == "http_500"
    assert failed_calls == 2
    assert failed_budget.summary(NOW.date().isoformat())["used"] == 2


def _reviewer(
    path: Path,
    *,
    post: Any,
    hard_limit: int = 168,
    now: Callable[[], datetime] = lambda: NOW,
) -> tuple[DeepSeekReviewer, DeepSeekBudgetStore]:
    budget = DeepSeekBudgetStore(
        path,
        daily_hard_limit=hard_limit,
        strategy_limits=_strategy_limits(hard_limit),
        stage_targets=_stage_targets(hard_limit),
        stage_limits=_stage_limits(hard_limit),
        challenger_limits={"today": 0, "tomorrow": 0, "d25": 0, "long": 0},
    )
    budget.initialize()
    weights = {
        "value_quality": 0.2,
        "financial_health": 0.2,
        "market_flow": 0.2,
        "industry_policy": 0.2,
        "risk_quality": 0.2,
    }
    reviewer = DeepSeekReviewer(
        _settings(hard_limit=hard_limit),
        budget,
        DeepSeekHttpClient(post=post, sleep=lambda _seconds: None),
        ReviewCache(),
        dimension_weights={strategy: weights for strategy in Strategy},
        strategy_version="c3-integration",
        confidence_coverage_min=0.5,
        minimum_known_dimensions=2,
        now=now,
    )
    return reviewer, budget


def _settings(*, hard_limit: int) -> DeepSeekSettings:
    return DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.example/v1",
        model="deepseek-v4-flash",
        challenger_model="deepseek-v4-pro",
        challenger_limits={"today": 0, "tomorrow": 0, "d25": 0, "long": 0},
        timeout_seconds=1.0,
        batch_size=8,
        max_tokens=256,
        daily_hard_limit=hard_limit,
        strategy_limits=_strategy_limits(hard_limit),
        stage_targets=_stage_targets(hard_limit),
        stage_limits=_stage_limits(hard_limit),
        api_key="secret",
    )


def _strategy_limits(hard_limit: int) -> dict[str, int]:
    if hard_limit == 168:
        return {"today": 68, "tomorrow": 45, "d25": 35, "long": 0, "shared_preheat": 15, "emergency": 5}
    return {"today": hard_limit, "tomorrow": 0, "d25": 0, "long": 0, "shared_preheat": 0, "emergency": 0}


def _stage_targets(hard_limit: int) -> dict[str, int]:
    return {stage: 0 for stage in _stage_limits(hard_limit)}


def _stage_limits(hard_limit: int) -> dict[str, int]:
    if hard_limit == 168:
        return {
            "today_main": 68,
            "tomorrow_afternoon": 45,
            "d25_afternoon": 35,
            "long_afternoon": 0,
            "shared_preheat": 15,
            "emergency": 5,
        }
    return {
        "today_main": hard_limit,
        "tomorrow_afternoon": 0,
        "d25_afternoon": 0,
        "long_afternoon": 0,
        "shared_preheat": 0,
        "emergency": 0,
    }


def _candidate(
    *,
    code: str = "600001",
    evidence_title: str = "交易所公告业绩改善",
) -> FeatureSnapshot:
    quote = MarketQuote(
        code=code,
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
        data_version="quote-v1",
    )
    return FeatureSnapshot(
        quote=quote,
        values={"relative_strength_5d": 65.0, "industry_policy_score": 60.0, "value_score": 55.0},
        observed_at=NOW,
        history_days=60,
        evidence=(Evidence("e-1", "announcement", evidence_title, "exchange", NOW - timedelta(hours=1), NOW, "v1"),),
    )


def _ok_response(code: str) -> _Response:
    return _Response(
        {
            "choices": [{"message": {"content": json.dumps(_payload(code))}, "finish_reason": "stop"}],
            "model": "deepseek-v4-flash-202607",
            "usage": {"total_tokens": 12},
        }
    )


def _payload(code: str) -> dict[str, object]:
    absent_risk = {"present": False, "severity": "low", "confidence": 0.0, "evidence_ids": []}
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
                    "evidence_ids": ["e-1"],
                },
                "price_reaction": {"bucket": "not_reflected", "evidence_ids": ["e-1"]},
                "fundamental": {"direction": "improving", "evidence_ids": ["e-1"]},
                "industry_policy": {"direction": "positive", "evidence_ids": ["e-1"]},
                "risks": {
                    "regulatory": absent_risk,
                    "shareholder_reduction": absent_risk,
                    "unlock": absent_risk,
                    "pledge": absent_risk,
                    "litigation": absent_risk,
                    "earnings": absent_risk,
                },
                "conflicts": [],
                "coverage": 0.9,
            }
        ],
    }
