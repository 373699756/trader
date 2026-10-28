"""Pure transitions of the bounded DeepSeek recovery circuit."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from trader.recommendation.infra.deepseek.budget_state import BudgetBatch, BudgetHealth, BudgetState

if TYPE_CHECKING:
    from trader.infra.settings.models import DeepSeekAdaptiveSettings


@dataclass(frozen=True)
class DeepSeekHealthPolicy:
    consecutive_failure_limit: int
    rolling_window: int
    minimum_application_ratio: float
    healthy_application_ratio: float
    healthy_batch_count: int
    cooldown_seconds: int

    @classmethod
    def from_settings(cls, settings: DeepSeekAdaptiveSettings) -> DeepSeekHealthPolicy:
        return cls(
            settings.consecutive_failure_limit, settings.rolling_window,
            settings.minimum_application_ratio, settings.healthy_application_ratio,
            settings.healthy_batch_count, settings.cooldown_seconds,
        )


def ReserveHealth(
    state: BudgetHealth, requested_at: datetime, batch_id: str, candidate_count: int
) -> tuple[BudgetHealth, str]:
    if state.mode in {"closed", "recovering"}:
        return state, ""
    if state.mode == "half_open":
        return state, "" if state.probe_batch_id == batch_id else "circuit_open"
    if state.open_until is None or requested_at < state.open_until:
        return state, "circuit_open"
    if not _half_open_allowed(requested_at) or not 1 <= candidate_count <= 2 or not batch_id:
        return state, "circuit_open"
    return replace(state, mode="half_open", probe_batch_id=batch_id), ""


def CompleteHealth(state: BudgetState, batch: BudgetBatch, policy: DeepSeekHealthPolicy) -> BudgetHealth:
    health = state.health_state
    if batch.budget_date != state.budget_date or batch.completed_at is None:
        return health
    calls = tuple(item for item in state.requests if item.batch_id == batch.batch_id)
    if not calls:
        return health
    valid = batch.applied + batch.abstain > 0 and any(item.status == "success" for item in calls)
    if health.mode == "half_open" and health.probe_batch_id == batch.batch_id:
        return BudgetHealth("recovering", recovery_successes=1) if valid else _open(
            batch.completed_at, "half_open_failed", policy
        )
    batches = sorted(
        (item for item in state.review_batches if item.budget_date == state.budget_date and item.completed_at),
        key=lambda item: item.completed_at or item.requested_at,
        reverse=True,
    )
    failures = batches[:policy.consecutive_failure_limit]
    reason = ""
    if len(failures) == policy.consecutive_failure_limit:
        if all(_transport_failed(state, item) for item in failures):
            reason = "consecutive_transport_failures"
        elif all(_schema_failed(state, item) for item in failures):
            reason = "consecutive_schema_failures"
    window = batches[:policy.rolling_window]
    candidates = sum(item.candidate_count for item in window)
    accepted = sum(item.applied + item.abstain for item in window)
    ratio = accepted / candidates if candidates else None
    if not reason and len(window) == policy.rolling_window and ratio is not None:
        if ratio < policy.minimum_application_ratio:
            reason = "low_application_ratio"
    if reason:
        return _open(batch.completed_at, reason, policy)
    if health.mode == "recovering":
        successes = health.recovery_successes + 1 if valid else 0
        if successes >= policy.healthy_batch_count and ratio is not None and ratio >= policy.healthy_application_ratio:
            return BudgetHealth()
        return replace(health, recovery_successes=successes)
    return health


def _transport_failed(state: BudgetState, batch: BudgetBatch) -> bool:
    calls = tuple(item for item in state.requests if item.batch_id == batch.batch_id)
    return any(item.status in {"failed", "abandoned"} for item in calls) and not any(
        item.status == "success" for item in calls
    )


def _schema_failed(state: BudgetState, batch: BudgetBatch) -> bool:
    return batch.status == "failed" and batch.applied + batch.abstain == 0 and any(
        item.batch_id == batch.batch_id and item.status == "success" for item in state.requests
    )


def _open(at: datetime, reason: str, policy: DeepSeekHealthPolicy) -> BudgetHealth:
    return BudgetHealth("open", at + timedelta(seconds=policy.cooldown_seconds), reason=reason)


def _half_open_allowed(value: datetime) -> bool:
    local = value.astimezone(ZoneInfo("Asia/Shanghai")).time().replace(tzinfo=None)
    return time(9, 15) <= local < time(15, 0)


__all__ = ["DeepSeekHealthPolicy", "ReserveHealth", "CompleteHealth"]
