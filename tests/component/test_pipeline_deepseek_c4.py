from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from trader.application.publisher import SnapshotPublisher
from trader.domain.market.models import Evidence, FeatureSnapshot, MarketQuote
from trader.domain.recommendation.downside import assess_downside
from trader.domain.recommendation.filters import hard_filter
from trader.domain.recommendation.models import (
    FusionMode,
    Recommendation,
    RecommendationAction,
    RecommendationSnapshot,
    ScoreBreakdown,
    Strategy,
)
from trader.domain.review.models import DeepSeekReview, ReviewCandidateContext, ReviewOutcome, RiskFact
from trader.infra.deepseek.budget import BudgetReservation, DeepSeekBudgetLedger
from trader.infra.deepseek.cache import ReviewCache
from trader.infra.deepseek.challenger import (
    ChallengerDimensionVerdict,
    ChallengerReview,
    merge_challenger_review,
)
from trader.infra.deepseek.client import DeepSeekHttpClient
from trader.infra.deepseek.evidence_router import route_prompt_evidence
from trader.infra.deepseek.health_gate import DeepSeekHealthPolicy
from trader.infra.deepseek.reviewer import DeepSeekReviewer
from trader.infra.deepseek.schema import classify_review, parse_reviews
from trader.infra.settings import DeepSeekSettings

NOW = datetime(2026, 7, 16, 6, 30, tzinfo=timezone.utc)


def test_c4_budget_enforces_normal_pro_and_emergency_daily_envelopes(tmp_path: Path) -> None:
    budget = _production_budget(tmp_path / "runtime.sqlite3")

    normal = (
        _reserve_many(budget, Strategy.TODAY, "today_main", 5)
        + _reserve_many(budget, Strategy.TODAY, "today_late", 3)
        + _reserve_many(budget, Strategy.TOMORROW, "today_main", 10)
        + _reserve_many(budget, Strategy.TOMORROW, "afternoon", 16)
        + _reserve_many(budget, Strategy.TOMORROW, "final_review", 10)
        + _reserve_many(budget, Strategy.D25, "today_main", 4)
        + _reserve_many(budget, Strategy.D25, "afternoon", 8)
        + _reserve_many(budget, Strategy.D25, "final_review", 4)
        + _reserve_many(budget, Strategy.TODAY, "warmup", 4, bucket="shared_preheat")
    )
    assert all(item.allowed for item in normal)
    assert len(normal) == 64

    challengers = _reserve_many(
        budget,
        Strategy.TOMORROW,
        "afternoon",
        2,
        model_role="challenger",
    )
    assert all(item.allowed for item in challengers)
    assert len(normal) + len(challengers) == 66

    emergencies = _reserve_many(
        budget,
        Strategy.TOMORROW,
        "final_review",
        5,
        emergency=True,
        emergency_reason="freeze_boundary_change",
    )
    assert all(item.allowed for item in emergencies)
    assert len(normal) + len(challengers) + len(emergencies) == 71
    assert (
        budget.reserve(
            Strategy.TOMORROW,
            phase="final_review",
            requested_at=NOW,
            emergency=True,
            emergency_reason="freeze_boundary_change",
        ).reason
        == "bucket_limit"
    )
    summary = budget.summary(NOW.date().isoformat())
    assert summary["used"] == 71
    assert summary["by_model_role"] == {"primary": 69, "challenger": 2}


def test_c4_global_168_limit_is_atomic_under_concurrent_reservations(tmp_path: Path) -> None:
    budget = DeepSeekBudgetLedger(
        tmp_path / "runtime.sqlite3",
        daily_hard_limit=168,
        strategy_limits={"c4_global_probe": 168},
        stage_targets={"today_main": 0},
        stage_limits={"today_main": 168},
        challenger_limits={"today": 0, "tomorrow": 0, "d25": 0},
    )
    budget.initialize()

    with ThreadPoolExecutor(max_workers=16) as executor:
        reservations = tuple(
            executor.map(
                lambda _index: budget.reserve(
                    Strategy.TODAY,
                    phase="today_main",
                    requested_at=NOW,
                    bucket="c4_global_probe",
                ),
                range(224),
            )
        )

    assert sum(item.allowed for item in reservations) == 168
    assert {item.reason for item in reservations if not item.allowed} == {"daily_hard_limit"}
    assert budget.summary(NOW.date().isoformat())["used"] == 168


def test_c4_rumors_duplicate_events_and_pro_cannot_relax_local_guards() -> None:
    rumor = _candidate(
        evidence=(
            Evidence(
                "rumor-1",
                "news",
                "市场传闻订单增长",
                "social_media",
                NOW - timedelta(hours=1),
                NOW,
                "rumor-v1",
            ),
        )
    )
    rumor_raw = parse_reviews(
        json.dumps(_v4_payload((rumor.quote.code,), evidence_ids=("rumor-1",))),
        (rumor,),
        NOW,
    )[rumor.quote.code]
    rumor_review = classify_review(
        rumor_raw,
        dimension_weights={
            "value_quality": 0.10,
            "financial_health": 0.10,
            "market_flow": 0.40,
            "industry_policy": 0.15,
            "risk_quality": 0.25,
        },
        confidence_coverage_min=0.50,
        minimum_known_dimensions=2,
    )
    assert rumor_review.outcome is ReviewOutcome.ABSTAIN
    assert rumor_review.dimensions["value_quality"].score == 50.0

    duplicated = _candidate(
        evidence=(
            Evidence(
                "duplicate-1",
                "announcement",
                "公司公告订单增长",
                "eastmoney_announcement",
                NOW - timedelta(hours=1),
                NOW - timedelta(minutes=1),
                "duplicate-v1",
            ),
            Evidence(
                "duplicate-2",
                "news",
                "公司公告订单增长",
                "eastmoney_news",
                NOW - timedelta(hours=1),
                NOW,
                "duplicate-v2",
            ),
        )
    )
    routed = route_prompt_evidence(duplicated).evidence
    assert tuple(item.evidence_id for item in routed) == ("duplicate-1",)
    duplicate_review = parse_reviews(
        json.dumps(_v4_payload((duplicated.quote.code,), evidence_ids=("duplicate-1",))),
        (duplicated,),
        NOW,
    )[duplicated.quote.code]
    assert duplicate_review.dimensions["value_quality"].score == 50.0

    guarded = _candidate(
        values={"pledge_risk": 1.0, "trend_breakdown": 1.0},
    )
    primary = parse_reviews(json.dumps(_v4_payload((guarded.quote.code,))), (guarded,), NOW)[guarded.quote.code]
    challenger = ChallengerReview(
        guarded.quote.code,
        {
            name: ChallengerDimensionVerdict("confirm", 1.0, dimension.evidence_ids, "confirmed")
            for name, dimension in primary.dimensions.items()
        },
        NOW,
    )
    merged = merge_challenger_review(primary, challenger, guarded)

    assert all(
        merged.dimensions[name].score <= primary.dimensions[name].score
        and merged.dimensions[name].confidence <= primary.dimensions[name].confidence
        for name in primary.dimensions
    )
    filter_result = hard_filter(guarded, NOW, max_age_seconds=20.0)
    assert filter_result.allowed is False
    assert "pledge_risk" in {item.filter_code for item in filter_result.reasons}
    assert assess_downside(guarded, Strategy.TODAY).status == "observe"


def test_c4_transport_failure_schema_repair_and_cache_count_physical_calls(tmp_path: Path) -> None:
    candidate = _candidate()
    retry_responses = iter((_Response({}, status_code=429), _ok_response((candidate.quote.code,))))
    retry_reviewer, retry_budget = _reviewer(
        tmp_path / "retry.sqlite3",
        post=lambda *_args, **_kwargs: next(retry_responses),
    )

    failed = retry_reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert failed[candidate.quote.code].outcome is ReviewOutcome.REJECTED
    assert retry_budget.summary(NOW.date().isoformat())["used"] == 1

    recovered = retry_reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert recovered[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    retry_summary = retry_budget.summary(NOW.date().isoformat())
    assert retry_summary["used"] == 2
    assert retry_summary["call_status"] == {"reserved": 0, "abandoned": 0, "failed": 1, "success": 1}

    repair_responses = iter((_content_response("not-json"), _ok_response((candidate.quote.code,))))
    repair_reviewer, repair_budget = _reviewer(
        tmp_path / "repair.sqlite3",
        post=lambda *_args, **_kwargs: next(repair_responses),
    )

    repaired = repair_reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert repaired[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    repair_summary = repair_budget.summary(NOW.date().isoformat())
    assert repair_summary["used"] == 2
    assert repair_summary["call_status"] == {"reserved": 0, "abandoned": 0, "failed": 0, "success": 2}

    cache_calls = 0

    def cached_post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal cache_calls
        cache_calls += 1
        return _ok_response((candidate.quote.code,))

    cache_reviewer, cache_budget = _reviewer(tmp_path / "cache.sqlite3", post=cached_post)
    for strategy, phase in (
        (Strategy.TODAY, "today_main"),
        (Strategy.TOMORROW, "afternoon"),
        (Strategy.D25, "afternoon"),
    ):
        result = cache_reviewer.review(
            strategy,
            (candidate,),
            phase=phase,
            deadline=NOW + timedelta(minutes=1),
        )
        assert result[candidate.quote.code].outcome is ReviewOutcome.APPLIED

    assert cache_calls == 1
    assert cache_budget.summary(NOW.date().isoformat())["used"] == 1


def test_c4_reviewer_enters_emergency_only_after_normal_bucket_limit(tmp_path: Path) -> None:
    reviewer, budget = _reviewer(
        tmp_path / "emergency.sqlite3",
        post=lambda *_args, **_kwargs: _ok_response(("600099",)),
    )
    assert all(item.allowed for item in _reserve_many(budget, Strategy.TODAY, "today_main", 5))
    assert all(item.allowed for item in _reserve_many(budget, Strategy.TODAY, "today_late", 3))
    blocked_candidate = _candidate(code="600098")
    blocked = reviewer.review(
        Strategy.TODAY,
        (blocked_candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )
    assert blocked[blocked_candidate.quote.code].error == "bucket_limit"
    acceptance = reviewer.status()["physical_call_acceptance"]
    assert isinstance(acceptance, Mapping)
    assert acceptance["zero_call_reason"] == "bucket_limit"
    candidate = _candidate(
        code="600099",
        external_risk_facts=(
            RiskFact(
                risk_fact_id="new-high-risk",
                risk_code="regulatory_risk",
                severity="high",
                penalty=0.0,
                source="exchange",
                observed_at=NOW,
                confidence=0.9,
                evidence_ids=("e-1",),
            ),
        ),
    )

    result = reviewer.review(
        Strategy.TODAY,
        (candidate,),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert result[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    summary = budget.summary(NOW.date().isoformat())
    assert summary["used"] == 9
    assert summary["by_bucket"] == {"today": 8, "emergency": 1}


def test_c4_emergency_reason_does_not_leak_to_later_ordinary_batch(tmp_path: Path) -> None:
    calls = 0

    def post(*_args: Any, **kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        request_payload = kwargs["json"]
        assert isinstance(request_payload, dict)
        messages = request_payload["messages"]
        assert isinstance(messages, list)
        user_message = messages[1]
        assert isinstance(user_message, dict)
        content = user_message["content"]
        assert isinstance(content, str)
        dynamic_payload = json.loads(content.rsplit("动态候选JSON=", 1)[1])
        codes = tuple(item["code"] for item in dynamic_payload["candidates"])
        return _ok_response(codes)

    reviewer, budget = _reviewer(tmp_path / "emergency-scope.sqlite3", post=post)
    assert all(item.allowed for item in _reserve_many(budget, Strategy.TODAY, "today_main", 5))
    assert all(item.allowed for item in _reserve_many(budget, Strategy.TODAY, "today_late", 3))
    high_risk = _candidate(
        code="600100",
        external_risk_facts=(
            RiskFact(
                risk_fact_id="new-high-risk",
                risk_code="regulatory_risk",
                severity="high",
                penalty=0.0,
                source="exchange",
                observed_at=NOW,
                confidence=0.9,
                evidence_ids=("e-1",),
            ),
        ),
    )
    ordinary = tuple(_candidate(code=f"600{code}") for code in range(101, 109))

    results = reviewer.review(
        Strategy.TODAY,
        (high_risk, *ordinary),
        phase="today_main",
        deadline=NOW + timedelta(minutes=1),
    )

    assert calls == 1
    assert results[high_risk.quote.code].outcome is ReviewOutcome.APPLIED
    assert all(results[candidate.quote.code].outcome is ReviewOutcome.APPLIED for candidate in ordinary[:3])
    assert all(candidate.quote.code in results for candidate in ordinary)
    assert all(results[candidate.quote.code].error == "bucket_limit" for candidate in ordinary[3:])
    assert budget.summary(NOW.date().isoformat())["by_bucket"] == {"today": 8, "emergency": 1}


def test_c4_challenger_daily_exhaustion_is_classified_as_budget_exhausted(tmp_path: Path) -> None:
    calls = 0

    def post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _ok_response(("600097",))

    reviewer, budget = _reviewer(
        tmp_path / "challenger.sqlite3",
        post=post,
        challenger_limits={"today": 0, "tomorrow": 2, "d25": 0},
    )
    assert all(
        item.allowed for item in (*_reserve_many(budget, Strategy.TOMORROW, "afternoon", 2, model_role="challenger"),)
    )
    candidate = _candidate(code="600097")
    result = reviewer.review(
        Strategy.TOMORROW,
        (candidate,),
        phase="afternoon",
        deadline=NOW + timedelta(minutes=1),
        contexts={
            candidate.quote.code: ReviewCandidateContext(
                local_score=70.0,
                local_rank=1,
                action_threshold=70.0,
                in_protection_set=True,
            )
        },
    )

    assert result[candidate.quote.code].outcome is ReviewOutcome.APPLIED
    assert result[candidate.quote.code].challenger_status == "budget_exhausted"
    assert calls == 1
    assert budget.summary(NOW.date().isoformat())["by_model_role"] == {"primary": 1, "challenger": 2}


def test_c4_deepseek_result_republication_p95_is_within_one_second() -> None:
    candidates = tuple(_candidate(code=f"600{index:03d}") for index in range(1, 9))
    reviews = parse_reviews(json.dumps(_v4_payload(tuple(item.quote.code for item in candidates))), candidates, NOW)
    recommendations = tuple(_recommendation(candidate, reviews[candidate.quote.code]) for candidate in candidates)
    publisher = SnapshotPublisher(history_size=64, client_queue_size=2)
    latencies: list[float] = []

    for sequence in range(30):
        snapshot = RecommendationSnapshot(
            snapshot_id=f"c4-{sequence}",
            strategy=Strategy.TODAY,
            trade_date="2026-07-16",
            phase="today_main",
            data_version=f"c4-{sequence}",
            strategy_version="c4",
            fusion_version="fusion_local68_deepseek32",
            fusion_mode=FusionMode.HYBRID,
            published_at=NOW,
            recommendations=recommendations,
            filtered_count=0,
            filter_reasons={},
        )
        started = perf_counter()
        event = publisher.publish(snapshot)
        latencies.append(perf_counter() - started)
        assert event.event_type == "recommendation_patch"

    p95 = sorted(latencies)[28]
    assert p95 <= 1.0


def test_c4_health_gate_opens_after_two_transport_failures(tmp_path: Path) -> None:
    calls = 0

    def failed_post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _Response({}, status_code=503)

    reviewer, budget = _reviewer(tmp_path / "health.sqlite3", post=failed_post)
    for code in ("600201", "600202"):
        result = reviewer.review(
            Strategy.TOMORROW,
            (_candidate(code=code),),
            phase="afternoon",
            deadline=NOW + timedelta(minutes=1),
        )
        assert result[code].outcome is ReviewOutcome.REJECTED

    blocked = reviewer.review(
        Strategy.TOMORROW,
        (_candidate(code="600203"),),
        phase="afternoon",
        deadline=NOW + timedelta(minutes=1),
    )

    assert blocked["600203"].error == "circuit_open"
    assert calls == 2
    assert budget.summary(NOW.date().isoformat())["health"] == {
        "mode": "open",
        "open_until": (NOW + timedelta(minutes=15)).isoformat(),
        "recovery_successes": 0,
        "reason": "consecutive_transport_failures",
    }


def test_c4_health_gate_persists_and_requires_three_valid_recovery_batches(tmp_path: Path) -> None:
    database_path = tmp_path / "health-recovery.sqlite3"
    current = [datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))]
    failed, _budget = _reviewer(
        database_path,
        post=lambda *_args, **_kwargs: _Response({}, status_code=503),
        now=lambda: current[0],
    )
    for code in ("600211", "600212"):
        failed.review(
            Strategy.TOMORROW,
            (_candidate(code=code),),
            phase="afternoon",
            deadline=current[0] + timedelta(minutes=1),
        )

    current[0] += timedelta(minutes=15)
    response_codes = iter(("600213", "600214", "600215"))
    recovered, recovered_budget = _reviewer(
        database_path,
        post=lambda *_args, **_kwargs: _ok_response((next(response_codes),)),
        now=lambda: current[0],
    )

    for recovery_count, code in enumerate(("600213", "600214", "600215"), start=1):
        result = recovered.review(
            Strategy.TOMORROW,
            (_candidate(code=code),),
            phase="afternoon",
            deadline=current[0] + timedelta(minutes=1),
        )
        assert result[code].outcome is ReviewOutcome.APPLIED
        health = recovered_budget.summary(current[0].date().isoformat())["health"]
        assert isinstance(health, Mapping)
        assert health["mode"] == ("closed" if recovery_count == 3 else "recovering")
        assert health["recovery_successes"] == (0 if recovery_count == 3 else recovery_count)


def test_c4_health_gate_does_not_half_open_after_afternoon_cutoff(tmp_path: Path) -> None:
    current = [datetime(2026, 7, 16, 14, 28, tzinfo=timezone(timedelta(hours=8)))]
    calls = 0

    def failed_post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        return _Response({}, status_code=503)

    reviewer, budget = _reviewer(
        tmp_path / "health-cutoff.sqlite3",
        post=failed_post,
        now=lambda: current[0],
    )
    for code in ("600221", "600222"):
        reviewer.review(
            Strategy.TOMORROW,
            (_candidate(code=code),),
            phase="afternoon",
            deadline=current[0] + timedelta(minutes=1),
        )

    current[0] += timedelta(minutes=15)
    blocked = reviewer.review(
        Strategy.TOMORROW,
        (_candidate(code="600223"),),
        phase="afternoon",
        deadline=current[0] + timedelta(minutes=1),
    )

    assert blocked["600223"].error == "circuit_open"
    assert calls == 2
    assert budget.summary(current[0].date().isoformat())["health"]["mode"] == "open"


def test_c4_health_gate_allows_only_one_concurrent_half_open_probe(tmp_path: Path) -> None:
    current = [datetime(2026, 7, 16, 13, 30, tzinfo=timezone(timedelta(hours=8)))]
    reviewer, _budget = _reviewer(
        tmp_path / "health-probe.sqlite3",
        post=lambda *_args, **_kwargs: _Response({}, status_code=503),
        now=lambda: current[0],
    )
    for code in ("600231", "600232"):
        reviewer.review(
            Strategy.TOMORROW,
            (_candidate(code=code),),
            phase="afternoon",
            deadline=current[0] + timedelta(minutes=1),
        )

    current[0] += timedelta(minutes=15)
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def delayed_post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5.0)
        return _ok_response(("600233",))

    probe_reviewer, probe_budget = _reviewer(
        tmp_path / "health-probe.sqlite3",
        post=delayed_post,
        now=lambda: current[0],
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            probe_reviewer.review,
            Strategy.TOMORROW,
            (_candidate(code="600233"),),
            phase="afternoon",
            deadline=current[0] + timedelta(minutes=1),
        )
        assert started.wait(timeout=5.0)
        second = executor.submit(
            probe_reviewer.review,
            Strategy.TOMORROW,
            (_candidate(code="600234"),),
            phase="afternoon",
            deadline=current[0] + timedelta(minutes=1),
        )
        second_result = second.result()
        release.set()
        first_result = first.result()

    assert first_result["600233"].outcome is ReviewOutcome.APPLIED
    assert second_result["600234"].error == "circuit_open"
    assert calls == 1
    assert probe_budget.summary(current[0].date().isoformat())["health"]["mode"] == "recovering"


def test_c4_submission_cutoffs_reject_new_today_and_tomorrow_calls(tmp_path: Path) -> None:
    for strategy, now, phase, deadline in (
        (
            Strategy.TODAY,
            datetime(2026, 7, 16, 11, 18, tzinfo=timezone(timedelta(hours=8))),
            "today_late",
            datetime(2026, 7, 16, 11, 20, tzinfo=timezone(timedelta(hours=8))),
        ),
        (
            Strategy.TOMORROW,
            datetime(2026, 7, 16, 11, 18, tzinfo=timezone(timedelta(hours=8))),
            "today_late",
            datetime(2026, 7, 16, 11, 20, tzinfo=timezone(timedelta(hours=8))),
        ),
        (
            Strategy.TOMORROW,
            datetime(2026, 7, 16, 14, 46, tzinfo=timezone(timedelta(hours=8))),
            "final_review",
            datetime(2026, 7, 16, 14, 48, tzinfo=timezone(timedelta(hours=8))),
        ),
    ):
        reviewer, budget = _reviewer(
            tmp_path / f"{strategy.value}-cutoff.sqlite3",
            post=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not call")),
            now=lambda now=now: now,
        )

        result = reviewer.review(strategy, (_candidate(),), phase=phase, deadline=deadline)

        assert result["600001"].error == "deadline_reached"
        assert budget.summary(now.date().isoformat())["used"] == 0


def test_c4_inflight_result_after_submission_cutoff_is_accepted_before_deadline(tmp_path: Path) -> None:
    current = [datetime(2026, 7, 16, 11, 17, 59, tzinfo=timezone(timedelta(hours=8)))]
    deadline = datetime(2026, 7, 16, 11, 20, tzinfo=timezone(timedelta(hours=8)))

    def post(*_args: Any, **_kwargs: Any) -> _Response:
        current[0] = datetime(2026, 7, 16, 11, 19, tzinfo=timezone(timedelta(hours=8)))
        return _ok_response(("600001",))

    reviewer, budget = _reviewer(
        tmp_path / "inflight.sqlite3",
        post=post,
        now=lambda: current[0],
    )

    result = reviewer.review(
        Strategy.TOMORROW,
        (_candidate(),),
        phase="today_late",
        deadline=deadline,
    )

    assert result["600001"].outcome is ReviewOutcome.APPLIED
    assert budget.summary(current[0].date().isoformat())["used"] == 1


def test_c4_concurrent_cross_strategy_reviews_singleflight_raw_facts(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def delayed_post(*_args: Any, **_kwargs: Any) -> _Response:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=5.0)
        return _ok_response(("600001",))

    reviewer, budget = _reviewer(tmp_path / "singleflight.sqlite3", post=delayed_post)
    candidate = _candidate()
    with ThreadPoolExecutor(max_workers=2) as executor:
        today = executor.submit(
            reviewer.review,
            Strategy.TODAY,
            (candidate,),
            phase="today_main",
            deadline=NOW + timedelta(minutes=1),
        )
        assert started.wait(timeout=5.0)
        tomorrow = executor.submit(
            reviewer.review,
            Strategy.TOMORROW,
            (candidate,),
            phase="today_main",
            deadline=NOW + timedelta(minutes=1),
        )
        release.set()
        results = (today.result(), tomorrow.result())

    assert calls == 1
    assert budget.summary(NOW.date().isoformat())["used"] == 1
    assert all(result[candidate.quote.code].outcome is ReviewOutcome.APPLIED for result in results)


def _reserve_many(
    budget: DeepSeekBudgetLedger,
    strategy: Strategy,
    phase: str,
    count: int,
    *,
    bucket: str | None = None,
    emergency: bool = False,
    emergency_reason: str = "",
    model_role: str = "primary",
) -> tuple[BudgetReservation, ...]:
    return tuple(
        budget.reserve(
            strategy,
            phase=phase,
            requested_at=NOW,
            bucket=bucket,
            emergency=emergency,
            emergency_reason=emergency_reason,
            model_role=model_role,
        )
        for _index in range(count)
    )


def _production_budget(path: Path) -> DeepSeekBudgetLedger:
    budget = DeepSeekBudgetLedger(
        path,
        daily_hard_limit=168,
        strategy_limits={
            "today": 8,
            "tomorrow": 38,
            "d25": 16,
            "shared_preheat": 4,
            "emergency": 5,
        },
        stage_targets={
            "tomorrow_morning": 0,
            "today_main": 0,
            "today_late": 0,
            "tomorrow_afternoon": 0,
            "tomorrow_final": 0,
            "d25_morning": 0,
            "d25_afternoon": 0,
            "d25_final": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_limits={
            "today_main": 5,
            "today_late": 3,
            "tomorrow_morning": 10,
            "tomorrow_afternoon": 18,
            "tomorrow_final": 10,
            "d25_morning": 4,
            "d25_afternoon": 8,
            "d25_final": 4,
            "shared_preheat": 4,
            "emergency": 5,
        },
        challenger_limits={"today": 0, "tomorrow": 2, "d25": 0},
        challenger_daily_limit=2,
        health_policy=DeepSeekHealthPolicy(
            consecutive_failure_limit=2,
            rolling_window=5,
            minimum_application_ratio=0.4,
            healthy_application_ratio=0.6,
            healthy_batch_count=3,
            cooldown_seconds=900,
        ),
    )
    budget.initialize()
    return budget


class _Response:
    def __init__(self, payload: dict[str, object], *, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers: dict[str, str] = {}

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http_{self.status_code}")


def _reviewer(
    path: Path,
    *,
    post: Any,
    challenger_limits: dict[str, int] | None = None,
    now: Any = None,
) -> tuple[DeepSeekReviewer, DeepSeekBudgetLedger]:
    budget = _production_budget(path)
    weights = {
        "value_quality": 0.2,
        "financial_health": 0.2,
        "market_flow": 0.2,
        "industry_policy": 0.2,
        "risk_quality": 0.2,
    }
    settings = DeepSeekSettings(
        enabled=True,
        base_url="https://api.deepseek.example/v1",
        model="deepseek-v4-flash",
        challenger_model="deepseek-v4-pro",
        challenger_limits=challenger_limits or {"today": 0, "tomorrow": 0, "d25": 0},
        challenger_daily_limit=2,
        timeout_seconds=1.0,
        batch_size=4,
        max_tokens=256,
        daily_hard_limit=168,
        strategy_limits={
            "today": 8,
            "tomorrow": 38,
            "d25": 16,
            "shared_preheat": 4,
            "emergency": 5,
        },
        stage_targets={
            "tomorrow_morning": 0,
            "today_main": 0,
            "today_late": 0,
            "tomorrow_afternoon": 0,
            "tomorrow_final": 0,
            "d25_morning": 0,
            "d25_afternoon": 0,
            "d25_final": 0,
            "shared_preheat": 0,
            "emergency": 0,
        },
        stage_limits={
            "today_main": 5,
            "today_late": 3,
            "tomorrow_morning": 10,
            "tomorrow_afternoon": 18,
            "tomorrow_final": 10,
            "d25_morning": 4,
            "d25_afternoon": 8,
            "d25_final": 4,
            "shared_preheat": 4,
            "emergency": 5,
        },
        api_key="secret",
    )
    reviewer = DeepSeekReviewer(
        settings,
        budget,
        DeepSeekHttpClient(post=post, sleep=lambda _seconds: None),
        ReviewCache(),
        dimension_weights={strategy: weights for strategy in Strategy},
        strategy_version="c4",
        confidence_coverage_min=0.5,
        minimum_known_dimensions=2,
        now=now or (lambda: NOW),
    )
    return reviewer, budget


def _candidate(
    *,
    code: str = "600001",
    evidence: tuple[Evidence, ...] | None = None,
    values: dict[str, float] | None = None,
    external_risk_facts: tuple[RiskFact, ...] = (),
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
    base_values = {
        "amount_median_20d": 100_000_000.0,
        "atr20_pct": 1.0,
        "volatility_20d": 20.0,
        "max_drawdown_20d": 10.0,
        "low_volatility_score": 50.0,
        "low_drawdown_score": 50.0,
        "close_location": 60.0,
        "market_breadth": 60.0,
        "relative_strength_5d": 65.0,
    }
    return FeatureSnapshot(
        quote=quote,
        values={**base_values, **(values or {})},
        observed_at=NOW,
        history_days=60,
        external_risk_facts=external_risk_facts,
        evidence=evidence
        if evidence is not None
        else (
            Evidence(
                "e-1",
                "announcement",
                "交易所公告业绩改善",
                "exchange",
                NOW - timedelta(hours=1),
                NOW,
                "evidence-v1",
            ),
        ),
    )


def _content_response(content: str) -> _Response:
    return _Response(
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "model": "deepseek-v4-flash-202607",
            "usage": {"total_tokens": 12},
        }
    )


def _ok_response(codes: tuple[str, ...]) -> _Response:
    return _content_response(json.dumps(_v4_payload(codes)))


def _v4_payload(codes: tuple[str, ...], *, evidence_ids: tuple[str, ...] = ("e-1",)) -> dict[str, object]:
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
                    "evidence_ids": list(evidence_ids),
                },
                "price_reaction": {"bucket": "not_reflected", "evidence_ids": list(evidence_ids)},
                "fundamental": {"direction": "improving", "evidence_ids": list(evidence_ids)},
                "industry_policy": {"direction": "positive", "evidence_ids": list(evidence_ids)},
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
            for code in codes
        ],
    }


def _recommendation(candidate: FeatureSnapshot, review: DeepSeekReview) -> Recommendation:
    return Recommendation(
        strategy=Strategy.TODAY,
        features=candidate,
        score=ScoreBreakdown({}, 80.0, 0.0, 80.0, 65.0, 0.9, 0.0, 75.2, FusionMode.HYBRID, True),
        local_risk_facts=(),
        deepseek_risk_facts=(),
        review=review,
        action=RecommendationAction.EXECUTABLE,
        action_reason="threshold_met",
        veto=False,
    )
