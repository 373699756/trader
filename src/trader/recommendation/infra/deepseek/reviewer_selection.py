"""Pure reviewer selection, deadline and audit rules."""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from typing_extensions import Unpack

from trader.recommendation.domain.evidence.review import (
    DeepSeekReview,
    ReviewCandidateContext,
    ReviewOutcome,
    RiskFact,
)
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.budget import DeepSeekBudgetLedger
from trader.recommendation.infra.deepseek.completion_client_contract import DeepSeekCompletionClient, DeepSeekHttpResult
from trader.recommendation.infra.deepseek.schema import (
    BuildReviewManifestHash,
)

_SUCCESSFUL_CANDIDATE_OUTCOMES = frozenset({ReviewOutcome.APPLIED, ReviewOutcome.ABSTAIN})


class _AnnotateRequiredOptions(TypedDict):
    candidate: FeatureSnapshot
    review_stage: str
    requested_model: str
    actual_model: str | None
    thinking_mode: str


class _AnnotateOptionalOptions(TypedDict, total=False):
    model_role: str
    reasoning_effort: str | None
    system_fingerprint: str | None
    prompt_cache_hit_tokens: int | None
    prompt_cache_miss_tokens: int | None


class _AnnotateOptions(_AnnotateRequiredOptions, _AnnotateOptionalOptions):
    pass


class _PhysicalCallOptions(TypedDict):
    enabled: bool
    configured: bool
    candidate_count: int
    cache_hits: int
    batch_status: str
    last_error: str
    physical_attempts: int


@dataclass
class _ReservationTracker:
    budget: DeepSeekBudgetLedger
    strategy: Strategy
    phase: str
    deadline: datetime
    now: Callable[[], datetime]
    planned_bucket: str
    batch_id: str
    emergency_reason: str
    model_role: str
    requested_model: str
    reasoning_effort: str
    reservation_deadline: datetime | None = None
    candidate_count: int = 0
    reservation_ids: list[str] = field(default_factory=list)
    failure_reason: str = ""
    monotonic_deadline: float | None = None
    monotonic: Callable[[], float] = time.monotonic

    def remaining_seconds(self) -> float:
        remaining = (self.deadline - _in_deadline_timezone(self.now(), self.deadline)).total_seconds()
        if self.monotonic_deadline is not None:
            remaining = min(remaining, self.monotonic_deadline - self.monotonic())
        return max(0.0, remaining)

    def reserve(self) -> bool:
        requested_at = _in_deadline_timezone(self.now(), self.deadline)
        reservation_deadline = self.reservation_deadline or self.deadline
        if requested_at >= reservation_deadline or self.remaining_seconds() <= 0.0:
            self.failure_reason = "deadline_reached"
            return False
        reservation = self.budget.reserve(
            self.strategy,
            phase=self.phase,
            requested_at=requested_at,
            bucket=self.planned_bucket,
            emergency=self.planned_bucket == "emergency",
            emergency_reason=self.emergency_reason,
            batch_id=self.batch_id,
            model_role=self.model_role,
            requested_model=self.requested_model,
            reasoning_effort=self.reasoning_effort,
            candidate_count=self.candidate_count,
        )
        if (
            not reservation.allowed
            and reservation.reason == "bucket_limit"
            and self.planned_bucket == self.strategy.value
            and self.emergency_reason
        ):
            reservation = self.budget.reserve(
                self.strategy,
                phase=self.phase,
                requested_at=requested_at,
                emergency=True,
                emergency_reason=self.emergency_reason,
                batch_id=self.batch_id,
                model_role=self.model_role,
                requested_model=self.requested_model,
                reasoning_effort=self.reasoning_effort,
                candidate_count=self.candidate_count,
            )
        self.failure_reason = reservation.reason
        if reservation.allowed:
            self.reservation_ids.append(reservation.reservation_id)
        return reservation.allowed


def _terminal_review(
    candidate: FeatureSnapshot,
    outcome: ReviewOutcome,
    completed_at: datetime,
    error: str,
) -> DeepSeekReview:
    return DeepSeekReview(
        code=candidate.quote.code,
        outcome=outcome,
        dimensions={},
        risk_facts=(),
        completed_at=completed_at,
        error=error[:500],
    )


def _annotate_review(
    review: DeepSeekReview,
    **options: Unpack[_AnnotateOptions],
) -> DeepSeekReview:
    candidate = options["candidate"]
    actual_model = options["actual_model"]
    system_fingerprint = options.get("system_fingerprint")
    prompt_cache_hit_tokens = options.get("prompt_cache_hit_tokens")
    prompt_cache_miss_tokens = options.get("prompt_cache_miss_tokens")
    return replace(
        review,
        review_stage=options["review_stage"],
        requested_model=options["requested_model"] or None,
        actual_model=actual_model if actual_model is not None else review.actual_model,
        thinking_mode=options["thinking_mode"] or None,
        model_role=options.get("model_role", "primary"),
        reasoning_effort=options.get("reasoning_effort"),
        system_fingerprint=(system_fingerprint if system_fingerprint is not None else review.system_fingerprint),
        prompt_cache_hit_tokens=(
            prompt_cache_hit_tokens if prompt_cache_hit_tokens is not None else review.prompt_cache_hit_tokens
        ),
        prompt_cache_miss_tokens=(
            prompt_cache_miss_tokens if prompt_cache_miss_tokens is not None else review.prompt_cache_miss_tokens
        ),
        evidence_manifest_hash=review.evidence_manifest_hash or BuildReviewManifestHash(candidate),
    )


def _thinking_mode(client: DeepSeekCompletionClient, model: str) -> str:
    try:
        capabilities = client.capabilities(model)
        return "reasoning" if capabilities.requires_reasoning_roundtrip else "standard"
    except Exception:
        return "standard"


def _unique_candidates(candidates: Sequence[FeatureSnapshot]) -> tuple[FeatureSnapshot, ...]:
    unique: dict[str, FeatureSnapshot] = {}
    for candidate in candidates:
        code = candidate.quote.code
        if code in unique:
            raise ValueError(f"duplicate DeepSeek candidate code: {code}")
        unique[code] = candidate
    return tuple(unique.values())


def _has_callable_features(candidate: FeatureSnapshot) -> bool:
    if candidate.quote.price is None or candidate.quote.price <= 0 or not math.isfinite(candidate.quote.price):
        return False
    return any(raw is not None and math.isfinite(float(raw)) for raw in candidate.values.values())


def _automatic_emergency_reason(candidates: Sequence[FeatureSnapshot], phase: str) -> str:
    if any(_is_new_high_risk(fact) for candidate in candidates for fact in candidate.external_risk_facts):
        return "new_high_risk"
    if phase == "final_review":
        return "freeze_boundary_change"
    return ""


def _review_priority(
    candidate: FeatureSnapshot,
    *,
    was_seen: bool,
    context: ReviewCandidateContext | None,
) -> int:
    high_risk = bool(
        (context is not None and context.has_new_high_risk)
        or any(_is_new_high_risk(fact) for fact in candidate.external_risk_facts)
    )
    action_boundary = bool(context is not None and context.near_action_threshold)
    topk_boundary = bool(context is not None and context.near_global_boundary)
    direction_conflict = bool(context is not None and context.direction_conflict)
    evidence_conflict = bool(context is not None and context.evidence_conflict)
    ordered = (
        high_risk,
        action_boundary,
        topk_boundary,
        direction_conflict,
        evidence_conflict,
        not was_seen,
    )
    return next((priority for priority, matched in enumerate(ordered) if matched), len(ordered))


def _is_new_high_risk(fact: RiskFact) -> bool:
    return fact.severity == "high" and fact.confidence >= 0.7


def _aggregate_batch_status(statuses: Sequence[str], *, cache_hits: int) -> str:
    if not statuses:
        return "skipped"
    if all(status == "success" for status in statuses):
        return "success"
    if any(status in {"success", "partial"} for status in statuses) or cache_hits > 0:
        return "partial"
    if any(status == "failed" for status in statuses):
        return "failed"
    return "skipped"


def _combine_results(first: DeepSeekHttpResult, second: DeepSeekHttpResult) -> DeepSeekHttpResult:
    return DeepSeekHttpResult(
        content=second.content,
        status_code=second.status_code,
        attempts=first.attempts + second.attempts,
        timed_out=first.timed_out or second.timed_out,
        error=second.error,
        usage=second.usage,
        attempt_records=first.attempt_records + second.attempt_records,
        actual_model=second.actual_model,
        system_fingerprint=second.system_fingerprint,
        finish_reason=second.finish_reason,
        prompt_cache_hit_tokens=second.prompt_cache_hit_tokens,
        prompt_cache_miss_tokens=second.prompt_cache_miss_tokens,
        reasoning_content=second.reasoning_content,
    )


def _usage_integer(usage: Mapping[str, object], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))


def _in_deadline_timezone(value: datetime, deadline: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("reviewer clock must return timezone-aware datetimes")
    if deadline.tzinfo is None:
        raise ValueError("DeepSeek deadline must be timezone-aware")
    return value.astimezone(deadline.tzinfo)


def _physical_call_acceptance(
    **options: Unpack[_PhysicalCallOptions],
) -> Mapping[str, object]:
    enabled = options["enabled"]
    configured = options["configured"]
    candidate_count = options["candidate_count"]
    cache_hits = options["cache_hits"]
    batch_status = options["batch_status"]
    last_error = options["last_error"]
    physical_attempts = options["physical_attempts"]
    applicable = enabled and configured and candidate_count > 0 and cache_hits < candidate_count
    if physical_attempts > 0:
        reason = ""
    elif not enabled:
        reason = "disabled"
    elif not configured:
        reason = "api_key_missing"
    elif candidate_count == 0:
        reason = "no_eligible_candidates"
    elif cache_hits >= candidate_count:
        reason = "all_candidates_cached"
    elif last_error in {
        "budget_exhausted",
        "bucket_limit",
        "stage_limit",
        "daily_hard_limit",
        "circuit_open",
    }:
        reason = last_error
    elif last_error == "deadline_reached":
        reason = "deadline_reached"
    elif batch_status == "skipped":
        reason = last_error or "batch_skipped"
    else:
        reason = "no_physical_attempt_recorded"
    return {
        "applicable": applicable,
        "passed": physical_attempts > 0 if applicable else None,
        "physical_attempts_last_batch": physical_attempts,
        "zero_call_reason": reason,
    }
