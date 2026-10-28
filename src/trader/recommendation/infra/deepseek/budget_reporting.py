"""Read-only projection from the one immutable budget state."""

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from trader.recommendation.application.ports.json_values import FreezeJsonObject, JsonInput, JsonObject
from trader.recommendation.infra.deepseek.budget_state import BudgetBatch, BudgetHealth, BudgetState


@dataclass(frozen=True)
class BudgetReportingConfig:
    daily_hard_limit: int
    daily_target: int
    stage_targets: Mapping[str, int]
    stage_limits: Mapping[str, int]


def ProjectBudgetSummary(
    state: BudgetState | None,
    transient: tuple[BudgetBatch, ...],
    day: str,
    config: BudgetReportingConfig,
    error: str,
) -> JsonObject:
    requests = tuple(item for item in state.requests if item.budget_date.isoformat() == day) if state else ()
    batches = tuple(item for item in (*state.review_batches, *transient) if item.budget_date.isoformat() == day) if state else tuple(
        item for item in transient if item.budget_date.isoformat() == day
    )
    by_bucket = Counter(item.bucket for item in requests)
    by_stage = Counter(item.stage for item in requests)
    by_status = Counter(item.status for item in requests)
    batch_status = Counter(item.status for item in batches)
    used = len(requests)
    normal_limit = sum(limit for stage, limit in config.stage_limits.items() if stage != "emergency")
    normal_used = used - by_bucket.get("emergency", 0)
    planned_limit = sum(config.stage_limits.values())
    health = state.health_state if state and state.budget_date.isoformat() == day else BudgetHealth()
    return FreezeJsonObject(cast(dict[str, JsonInput], {
        "available": not error,
        "error": error,
        "budget_date": state.budget_date.isoformat() if state else None,
        "revision": state.revision if state else 0,
        "budget_policy_hash": state.budget_policy_hash if state else "",
        "used": used,
        "remaining": max(0, config.daily_hard_limit - used) if not error else 0,
        "target": config.daily_target,
        "target_met": all(by_stage[name] >= target for name, target in config.stage_targets.items() if name != "emergency"),
        "normal_limit": normal_limit,
        "normal_used": normal_used,
        "normal_remaining": max(0, normal_limit - normal_used) if not error else 0,
        "planned_limit": planned_limit,
        "planned_remaining": max(0, planned_limit - used) if not error else 0,
        "by_bucket": dict(by_bucket),
        "by_strategy": dict(Counter(item.strategy for item in requests if item.bucket != "shared_preheat")),
        "by_stage": {
            name: {
                "used": by_stage[name], "target": config.stage_targets[name], "limit": limit,
                "remaining": max(0, limit - by_stage[name]) if not error else 0,
                "target_met": by_stage[name] >= config.stage_targets[name],
            }
            for name, limit in config.stage_limits.items()
        },
        "by_status": dict(by_status),
        "call_status": {name: by_status[name] for name in ("reserved", "success", "failed", "abandoned")},
        "batch_status": {name: batch_status[name] for name in ("running", "success", "partial", "failed", "skipped", "abandoned")},
        "candidate_outcomes": {
            "applied": sum(item.applied for item in batches), "abstain": sum(item.abstain for item in batches),
            "rejected": sum(item.rejected for item in batches), "late": sum(item.late for item in batches),
        },
        "by_model_role": {"primary": used, "challenger": 0},
        "http_429_count": sum(item.http_status == 429 for item in requests),
        "timeout_count": sum(item.timed_out for item in requests),
        "token_count": sum(item.token_count for item in requests),
        "health": {
            "mode": health.mode, "open_until": health.open_until.isoformat() if health.open_until else None,
            "recovery_successes": health.recovery_successes, "reason": health.reason,
        },
    }))


__all__ = ["BudgetReportingConfig", "ProjectBudgetSummary"]
