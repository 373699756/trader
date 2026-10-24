"""Reusable review facts retain their request provenance and cannot cross input changes."""

from __future__ import annotations

import json as json_codec
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest

from tests.component.test_deepseek import _reviewer_policy, _settings
from tests.component.test_deepseek_schema import NOW, _budget, _candidate, _primary_payload, _Response
from tests.unit.application.test_recommendation_boundaries import _features, _native_input, _runtime_policy
from trader.infra.http_contracts import HttpTimeout
from trader.recommendation.application.pipeline.final_selection.decision_projection import (
    BuildScoredHybrid,
    ValidateReviewManifests,
)
from trader.recommendation.application.pipeline.local_score.base_scoring import LocalScoringService
from trader.recommendation.application.pipeline.risk_review.deepseek_evidence_gate import NormalizeScoredReviewTimes
from trader.recommendation.domain.evidence.review import DeepSeekReview, ReviewOutcome
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.cache import BuildSharedReviewCacheIdentity, ReviewCache
from trader.recommendation.infra.deepseek.client import DeepSeekHttpClient
from trader.recommendation.infra.deepseek.reviewer import DeepSeekReviewer
from trader.recommendation.infra.deepseek.schema_prompts import BuildReviewManifestHash, ReviewCacheKey


def test_reused_review_binds_current_input_without_rewriting_request_manifest(application_feature_factory):
    policy = _runtime_policy()
    projection = LocalScoringService().score(_native_input(_features(application_feature_factory)), policy, sequence=1)
    current = projection.review_candidates[0].features
    original = replace(current, observed_at=current.observed_at - timedelta(seconds=1))
    review = DeepSeekReview(
        current.quote.code,
        ReviewOutcome.ABSTAIN,
        {},
        (),
        original.observed_at,
        evidence_manifest_hash=BuildReviewManifestHash(original),
    )
    cache = ReviewCache()
    key = ReviewCacheKey(original, model="deepseek-v4-flash")
    assert key == ReviewCacheKey(current, model="deepseek-v4-flash")
    cache.put_raw(key, original, review)
    rebound = cache.get_bound_raw(key, current, bound_at=projection.native_input.evaluated_at)
    assert rebound is not None and rebound.evidence_binding is not None
    assert rebound.evidence_manifest_hash == review.evidence_manifest_hash != BuildReviewManifestHash(current)
    expected = {current.quote.code: BuildReviewManifestHash(current)}
    assert ValidateReviewManifests(projection, {current.quote.code: rebound}, expected)
    hybrid = BuildScoredHybrid(
        projection,
        policy,
        {current.quote.code: rebound},
        review_deadline=projection.native_input.evaluated_at + timedelta(minutes=1),
    )
    assert hybrid is not None and hybrid.decision is not None
    assert tuple((item.code, item.final_score) for item in hybrid.decision.items) == tuple(
        (item.code, item.final_score) for item in projection.local.items
    )
    for field in ("original_manifest_hash", "current_manifest_hash"):
        tampered = replace(rebound, evidence_binding=replace(rebound.evidence_binding, **{field: "f" * 64}))
        assert not ValidateReviewManifests(projection, {current.quote.code: tampered}, expected)
    late = replace(
        rebound,
        evidence_binding=replace(rebound.evidence_binding, bound_at=current.observed_at + timedelta(seconds=60)),
    )
    normalized = NormalizeScoredReviewTimes({current.quote.code: late}, current.observed_at + timedelta(seconds=60))
    assert normalized is not None and normalized[current.quote.code].outcome is ReviewOutcome.LATE


@pytest.mark.parametrize("change", ("expired", "price", "volume", "evidence"))
def test_raw_cache_reuse_rejects_expiry_quote_thresholds_and_missing_evidence(change):
    candidate = _candidate()
    dimension_payload = _primary_payload()
    from trader.recommendation.infra.deepseek.schema import ParseReviews

    review = ParseReviews(json_codec.dumps(dimension_payload), (candidate,), NOW)[candidate.quote.code]
    cache = ReviewCache(monotonic=lambda: 0.0)
    key = ReviewCacheKey(candidate, model="deepseek-v4-flash")
    cache.put_raw(key, candidate, review)
    bound_at = NOW + timedelta(seconds=600 if change == "expired" else 10)
    current = replace(candidate, observed_at=bound_at)
    if change == "price":
        current = replace(current, quote=replace(current.quote, price=12.12))
    elif change == "volume":
        current = replace(current, quote=replace(current.quote, volume_ratio=2.3))
    elif change == "evidence":
        current = replace(current, evidence=())
    assert cache.get_bound_raw(key, current, bound_at=bound_at) is None


def test_shared_cache_identity_ignores_credentials_and_binds_model_contract():
    settings = _settings()
    assert BuildSharedReviewCacheIdentity(settings) == BuildSharedReviewCacheIdentity(
        replace(settings, api_key="other")
    )
    assert BuildSharedReviewCacheIdentity(settings) != BuildSharedReviewCacheIdentity(
        replace(settings, max_tokens=settings.max_tokens + 1)
    )


def test_crossed_profile_claims_execute_owned_work_before_waiting(tmp_path):
    barrier = threading.Barrier(2)

    class CrossedClaimCache(ReviewCache):
        def __init__(self):
            super().__init__()
            self.visited = set()
            self.visited_lock = threading.Lock()

        def claim_raw(self, key):
            owned = super().claim_raw(key)
            if owned:
                with self.visited_lock:
                    first = threading.get_ident() not in self.visited
                    self.visited.add(threading.get_ident())
                if first:
                    barrier.wait(timeout=10.0)
            return owned

        def wait_raw(self, key, timeout_seconds):
            return super().wait_raw(key, min(timeout_seconds, 10.0))

    cache = CrossedClaimCache()
    budget = _budget(tmp_path / "budget.sqlite3", hard_limit=4)
    settings = replace(_settings(), batch_size=1, timeout_seconds=0.5)
    calls = []
    calls_lock = threading.Lock()

    def post(_url, *, headers, json: dict, timeout):
        del headers
        assert 0.0 < timeout <= 0.5
        candidates = json_codec.loads(json["messages"][-1]["content"].split("动态候选JSON=")[1])["candidates"]
        payload = _primary_payload()
        payload["results"][0]["code"] = candidates[0]["code"]
        with calls_lock:
            calls.append(candidates[0]["code"])
        return _Response({"choices": [{"message": {"content": json_codec.dumps(payload)}, "finish_reason": "stop"}]})

    def reviewer(version):
        return DeepSeekReviewer(
            settings,
            budget,
            DeepSeekHttpClient(post=post),
            cache,
            **{**_reviewer_policy(), "strategy_version": version, "now": lambda: NOW},
        )

    first = _candidate()
    second = replace(first, quote=replace(first.quote, code="600002"))
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(
                reviewer("profile-v2").review,
                Strategy.TOMORROW,
                (first, second),
                phase="morning_main",
                deadline=NOW + timedelta(seconds=30),
            ),
            executor.submit(
                reviewer("profile-v3").review,
                Strategy.TOMORROW,
                (second, first),
                phase="morning_main",
                deadline=NOW + timedelta(seconds=30),
            ),
        )
        results = tuple(future.result(timeout=30.0) for future in futures)
    assert sorted(calls) == ["600001", "600002"]
    assert all(set(result) == {"600001", "600002"} for result in results)
    assert all(
        review.outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
        for result in results
        for review in result.values()
    )
    assert sum(review.cache_reused for result in results for review in result.values()) == 2
    assert budget.summary(NOW.date().isoformat())["used"] == 2


def test_monotonic_deadline_clamps_http_and_failed_owner_releases_claim(tmp_path):
    clock = [0.0]
    calls = []
    cache = ReviewCache()
    budget = _budget(tmp_path / "budget.sqlite3", hard_limit=4)

    def post(_url, *, headers, json, timeout):
        del headers, json
        calls.append(timeout)
        assert 0.0 < timeout <= 5.0
        if len(calls) == 1:
            clock[0] += 6.0
            raise HttpTimeout("fixture")
        return _Response(
            {"choices": [{"message": {"content": json_codec.dumps(_primary_payload())}, "finish_reason": "stop"}]}
        )

    reviewer = DeepSeekReviewer(
        _settings(),
        budget,
        DeepSeekHttpClient(post=post),
        cache,
        **{**_reviewer_policy(), "now": lambda: NOW, "monotonic": lambda: clock[0]},
    )
    candidate = _candidate()
    first = reviewer.review(Strategy.TOMORROW, (candidate,), phase="morning_main", deadline=NOW + timedelta(seconds=5))
    assert first[candidate.quote.code].outcome is ReviewOutcome.LATE
    second = reviewer.review(Strategy.TOMORROW, (candidate,), phase="morning_main", deadline=NOW + timedelta(seconds=5))
    assert second[candidate.quote.code].outcome in {ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN}
    assert len(calls) == budget.summary(NOW.date().isoformat())["used"] == 2
