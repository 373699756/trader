"""Immutable, bounded recovery state for the shared physical-request budget."""

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class BudgetRequest:
    request_id: str
    budget_date: date
    profile: str
    strategy: str
    bucket: str
    stage: str
    batch_id: str
    emergency_reason: str
    requested_at: datetime
    status: str = "reserved"
    completed_at: datetime | None = None
    http_status: int | None = None
    token_count: int = 0
    timed_out: bool = False


@dataclass(frozen=True)
class BudgetBatch:
    batch_id: str
    budget_date: date
    profile: str
    strategy: str
    requested_at: datetime
    deadline: datetime
    candidate_count: int
    cache_hit_count: int = 0
    status: str = "running"
    completed_at: datetime | None = None
    applied: int = 0
    abstain: int = 0
    rejected: int = 0
    late: int = 0


@dataclass(frozen=True)
class BudgetHealth:
    mode: str = "closed"
    open_until: datetime | None = None
    probe_batch_id: str = ""
    recovery_successes: int = 0
    reason: str = ""


@dataclass(frozen=True)
class BudgetState:
    schema_version: int
    budget_date: date
    budget_policy_hash: str
    revision: int
    owner_identity: str
    blocked_date: date | None = None
    blocked_reason: str = ""
    requests: tuple[BudgetRequest, ...] = ()
    review_batches: tuple[BudgetBatch, ...] = ()
    health_state: BudgetHealth = BudgetHealth()


__all__ = ["BudgetRequest", "BudgetBatch", "BudgetHealth", "BudgetState"]
