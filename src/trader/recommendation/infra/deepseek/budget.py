"""Single shared owner for durable, fail-closed DeepSeek request budgets."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, TypedDict
from zoneinfo import ZoneInfo

from trader.infra.process_lock import ProcessLockError
from trader.infra.serialization.canonical_codec import CanonicalDigest
from trader.recommendation.application.ports.json_values import JsonObject
from trader.recommendation.domain.publication.models import Strategy
from trader.recommendation.infra.deepseek.budget_batch_ledger import BudgetBatchCompletion, BudgetBatchRequest
from trader.recommendation.infra.deepseek.budget_codec import BudgetDay
from trader.recommendation.infra.deepseek.budget_file import BudgetStateFile
from trader.recommendation.infra.deepseek.budget_reporting import BudgetReportingConfig, ProjectBudgetSummary
from trader.recommendation.infra.deepseek.budget_state import BudgetBatch, BudgetHealth, BudgetRequest, BudgetState
from trader.recommendation.infra.deepseek.health_gate import CompleteHealth, DeepSeekHealthPolicy, ReserveHealth

if TYPE_CHECKING:
    from typing_extensions import Unpack


@dataclass(frozen=True)
class BudgetReservation:
    allowed: bool
    reservation_id: str
    bucket: str
    reason: str
    stage: str = ""


class _ReserveRequiredOptions(TypedDict):
    phase: str
    requested_at: datetime


class _ReserveOptionalOptions(TypedDict, total=False):
    bucket: str | None
    emergency: bool
    emergency_reason: str
    batch_id: str
    model_role: str
    requested_model: str
    reasoning_effort: str
    candidate_count: int


class ReserveOptions(_ReserveRequiredOptions, _ReserveOptionalOptions):
    pass


class _FinishRequiredOptions(TypedDict):
    status: str


class _FinishOptionalOptions(TypedDict, total=False):
    error: str
    http_status: int | None
    latency_ms: float | None
    token_count: int
    timed_out: bool
    completed_at: datetime | None
    actual_model: str | None
    system_fingerprint: str | None
    finish_reason: str | None
    prompt_tokens: int
    completion_tokens: int
    prompt_cache_hit_tokens: int
    prompt_cache_miss_tokens: int


class FinishOptions(_FinishRequiredOptions, _FinishOptionalOptions):
    pass



@dataclass(frozen=True)
class _PendingBatch:
    batch: BudgetBatch
    codes: tuple[str, ...]


class DeepSeekBudgetLedger:
    def __init__(
        self,
        state_file: BudgetStateFile,
        *,
        daily_hard_limit: int,
        strategy_limits: Mapping[str, int],
        stage_targets: Mapping[str, int],
        stage_limits: Mapping[str, int],
        health_policy: DeepSeekHealthPolicy | None = None,
        enabled: bool = True,
    ) -> None:
        _validate_budget_envelopes(daily_hard_limit, strategy_limits, stage_targets, stage_limits)
        self._file = state_file
        self._limits = dict(strategy_limits)
        self._stage_limits = dict(stage_limits)
        self._policy = health_policy
        self._config = BudgetReportingConfig(daily_hard_limit, sum(stage_targets.values()), stage_targets, stage_limits)
        self._policy_hash = CanonicalDigest((daily_hard_limit, strategy_limits, stage_targets, stage_limits, health_policy))
        self._enabled = enabled
        self._lock = threading.RLock()
        self._summary_lock = threading.Lock()
        self._state: BudgetState | None = None
        self._pending: tuple[_PendingBatch, ...] = ()
        self._transient: tuple[BudgetBatch, ...] = ()
        self._error = "budget_uninitialized" if enabled else "disabled"
        self._initialized = False
        self._closed = False
        self._last_at: datetime | None = None
        self._summaries: tuple[tuple[str, JsonObject], ...] = ()
        self._empty_summary = ProjectBudgetSummary(None, (), "", self._config, self._error)

    def initialize(self, at: datetime) -> None:
        BudgetDay(at)
        with self._lock:
            if not self._enabled or self._closed:
                return
            if self._initialized:
                self._advance_day(at)
                return
            self._last_at = at
            try:
                self._state = self._file.open(at, self._policy_hash)
            except (OSError, ProcessLockError, ValueError):
                self._error = "budget_owner_unavailable"
                self._refresh()
                return
            self._initialized = True
            self._error = ""
            self.recover_incomplete(at)
            self._advance_day(at)
            # Even an idle restart must establish that this file is writable.
            if self._state is not None:
                self._commit(replace(self._state, budget_policy_hash=self._policy_hash))
            self._refresh()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._error = "budget_closed"
            self._file.close()
            self._refresh()

    def summary(self, day: str) -> JsonObject:
        # This path never takes the writer lock or touches the filesystem.
        with self._summary_lock:
            return next((value for key, value in self._summaries if key == day), self._empty_summary)

    def begin_batch(self, request: BudgetBatchRequest) -> str:
        day = BudgetDay(request.requested_at)
        BudgetDay(request.deadline)
        codes = tuple(request.candidate_codes)
        if len(codes) != len(set(codes)) or not 0 <= request.cache_hit_count <= len(codes):
            raise ValueError("invalid DeepSeek batch candidates or cache count")
        if request.profile not in {"v1", "v2", "v3", "v5"}:
            raise ValueError("invalid DeepSeek profile")
        batch = BudgetBatch(
            uuid.uuid4().hex, day, request.profile, request.strategy.value,
            request.requested_at, request.deadline, len(codes), request.cache_hit_count,
        )
        with self._lock:
            self._last_at = request.requested_at
            if len(self._pending) >= 256:
                raise ValueError("DeepSeek pending batch capacity exceeded")
            self._pending += (_PendingBatch(batch, codes),)
        return batch.batch_id

    def set_batch_cache_hits(self, batch_id: str, count: int) -> None:
        with self._lock:
            batch = self._batch(batch_id)
            if batch.status != "running" or not 0 <= count <= batch.candidate_count:
                raise KeyError("unknown batch or invalid cache hit count")
            self._update_batch(replace(batch, cache_hit_count=count))

    def finish_batch(self, completion: BudgetBatchCompletion) -> None:
        BudgetDay(completion.completed_at)
        if completion.status not in {"success", "partial", "failed", "skipped"} or completion.physical_attempts < 0:
            raise ValueError("invalid batch completion")
        with self._lock:
            batch = self._batch(completion.batch_id)
            if batch.status != "running":
                raise KeyError("unknown or completed DeepSeek batch")
            pending = next((item for item in self._pending if item.batch.batch_id == batch.batch_id), None)
            if pending is None:
                raise KeyError("batch no longer belongs to this process")
            reviews = tuple(completion.reviews.values())
            if any(code not in pending.codes or code != review.code for code, review in completion.reviews.items()):
                raise ValueError("DeepSeek candidate result does not match its batch code")
            outcomes = tuple(review.outcome.value for review in reviews)
            if any(value not in {"applied", "abstain", "rejected", "late"} for value in outcomes):
                raise ValueError("invalid candidate terminal outcome")
            batch = replace(
                batch, status=completion.status, completed_at=completion.completed_at,
                applied=outcomes.count("applied"), abstain=outcomes.count("abstain"),
                rejected=outcomes.count("rejected") + batch.candidate_count - len(reviews), late=outcomes.count("late"),
            )
            self._last_at = completion.completed_at
            self._update_batch(batch, complete_health=True)
            self._pending = tuple(item for item in self._pending if item.batch.batch_id != batch.batch_id)

    def fail_running_batch(self, batch_id: str, *, completed_at: datetime, error: str) -> bool:
        BudgetDay(completed_at)
        with self._lock:
            try:
                batch = self._batch(batch_id)
            except KeyError:
                return False
            if batch.status != "running":
                return False
            self._last_at = completed_at
            if self._state is not None:
                requests = tuple(
                    replace(item, status="abandoned", completed_at=completed_at)
                    if item.batch_id == batch_id and item.status == "reserved" else item
                    for item in self._state.requests
                )
                self._state = replace(self._state, requests=requests)
            self._update_batch(
                replace(batch, status="failed", completed_at=completed_at, rejected=batch.candidate_count),
                complete_health=True,
            )
            self._pending = tuple(item for item in self._pending if item.batch.batch_id != batch_id)
            return True

    def reserve(self, strategy: Strategy, **options: Unpack[ReserveOptions]) -> BudgetReservation:
        requested_at = options["requested_at"]
        day = BudgetDay(requested_at)
        bucket = "emergency" if options.get("emergency", False) else (options.get("bucket") or strategy.value)
        stage = _stage_key(strategy, options["phase"], bucket, requested_at)
        batch_id = options.get("batch_id", "")
        reason = ""
        if strategy not in {Strategy.TOMORROW, Strategy.D25} or strategy.value not in self._limits:
            reason = "unknown_strategy"
        elif bucket not in self._limits:
            reason = "unknown_bucket"
        elif options.get("model_role", "primary") != "primary":
            reason = "invalid_model_role"
        elif options.get("candidate_count", 0) < 0:
            reason = "invalid_candidate_count"
        elif stage not in self._stage_limits:
            reason = "unknown_stage"
        elif bucket == "emergency" and options.get("emergency_reason") not in {"new_high_risk", "freeze_boundary_change"}:
            reason = "invalid_emergency_reason"
        with self._lock:
            self._last_at = requested_at
            self._advance_day(requested_at)
            state = self._state
            reason = reason or self._unavailable(day)
            batch = self._batch(batch_id) if batch_id else None
            if batch is not None and (
                batch.status != "running"
                or batch.budget_date != day
                or batch.strategy != strategy.value
                or requested_at >= batch.deadline
            ):
                reason = reason or "batch_expired"
            if reason or state is None:
                return BudgetReservation(False, "", bucket, reason or "budget_uninitialized", stage)
            requests = tuple(item for item in state.requests if item.budget_date == day)
            if len(requests) >= self._config.daily_hard_limit:
                reason = "daily_hard_limit"
            elif sum(item.bucket == bucket for item in requests) >= self._limits[bucket]:
                reason = "bucket_limit"
            elif bucket == "emergency":
                if sum(item.bucket == strategy.value for item in requests) < self._limits[strategy.value]:
                    reason = "normal_budget_available"
            elif sum(item.stage == stage for item in requests) >= self._stage_limits[stage]:
                reason = "stage_limit"
            health = state.health_state
            if not reason and self._policy is not None:
                health, reason = ReserveHealth(health, requested_at, batch_id, options.get("candidate_count", 0))
            if reason:
                return BudgetReservation(False, "", bucket, reason, stage)
            request = BudgetRequest(
                uuid.uuid4().hex, day, batch.profile if batch else "v2", strategy.value, bucket, stage,
                batch_id, options.get("emergency_reason", ""), requested_at,
            )
            batches = state.review_batches
            if batch is not None and not any(item.batch_id == batch_id for item in batches):
                batches += (batch,)
            candidate = replace(state, requests=state.requests + (request,), review_batches=batches, health_state=health)
            if not self._commit(candidate):
                return BudgetReservation(False, "", bucket, self._error, stage)
            return BudgetReservation(True, request.request_id, bucket, "reserved", stage)

    def finish(self, reservation_id: str, **options: Unpack[FinishOptions]) -> None:
        if options["status"] not in {"success", "failed", "abandoned"}:
            raise ValueError("invalid physical call terminal status")
        with self._lock:
            state = self._state
            if self._closed or state is None:
                return
            completed = options.get("completed_at") or self._last_at
            if completed is None:
                raise ValueError("completion time required")
            BudgetDay(completed)
            request = next((item for item in state.requests if item.request_id == reservation_id), None)
            if request is None or request.status != "reserved":
                raise KeyError("unknown or completed reservation")
            updated = replace(
                request, status=options["status"], completed_at=completed, http_status=options.get("http_status"),
                token_count=max(0, options.get("token_count", 0)), timed_out=options.get("timed_out", False),
            )
            self._last_at = completed
            self._commit(replace(state, requests=tuple(updated if item == request else item for item in state.requests)))

    def recover_incomplete(self, recovered_at: datetime) -> int:
        BudgetDay(recovered_at)
        with self._lock:
            state = self._state
            if state is None or self._closed:
                return 0
            calls = sum(item.status == "reserved" for item in state.requests)
            batches = sum(item.status == "running" for item in state.review_batches)
            if not calls and not batches:
                return 0
            health = state.health_state
            if health.mode == "half_open":
                seconds = self._policy.cooldown_seconds if self._policy else 900
                health = BudgetHealth("open", recovered_at + timedelta(seconds=seconds), reason="probe_abandoned")
            candidate = replace(
                state,
                requests=tuple(
                    replace(item, status="abandoned", completed_at=recovered_at) if item.status == "reserved" else item
                    for item in state.requests
                ),
                review_batches=tuple(
                    replace(item, status="abandoned", completed_at=recovered_at, rejected=item.candidate_count)
                    if item.status == "running" else item for item in state.review_batches
                ),
                health_state=health,
            )
            self._last_at = recovered_at
            self._commit(candidate)
            return calls + batches

    def _batch(self, batch_id: str) -> BudgetBatch:
        if self._state is not None:
            for item in self._state.review_batches:
                if item.batch_id == batch_id:
                    return item
        for pending in self._pending:
            if pending.batch.batch_id == batch_id:
                return pending.batch
        for item in self._transient:
            if item.batch_id == batch_id:
                return item
        raise KeyError("unknown DeepSeek batch")

    def _update_batch(self, batch: BudgetBatch, *, complete_health: bool = False) -> None:
        state = self._state
        if state is not None and any(item.batch_id == batch.batch_id for item in state.review_batches):
            candidate = replace(
                state, review_batches=tuple(batch if item.batch_id == batch.batch_id else item for item in state.review_batches)
            )
            if complete_health and self._policy is not None:
                candidate = replace(candidate, health_state=CompleteHealth(candidate, batch, self._policy))
            self._commit(candidate)
        elif batch.status == "running":
            self._pending = tuple(
                replace(item, batch=batch) if item.batch.batch_id == batch.batch_id else item for item in self._pending
            )
        else:
            self._transient = (*self._transient[-167:], batch)
            self._refresh()

    def _advance_day(self, at: datetime) -> None:
        state = self._state
        day = BudgetDay(at)
        if state is None or day <= state.budget_date or self._closed:
            return
        # Keep complete old batches while any of their physical attempts is in flight.
        pending_ids = {item.batch_id for item in state.review_batches if item.status == "running"}
        pending_ids.update(item.batch_id for item in state.requests if item.status == "reserved" and item.batch_id)
        requests = tuple(item for item in state.requests if item.status == "reserved" or item.batch_id in pending_ids)
        batches = tuple(item for item in state.review_batches if item.batch_id in pending_ids)
        candidate = replace(
            state, budget_date=day, requests=requests, review_batches=batches, health_state=BudgetHealth(),
            budget_policy_hash=self._policy_hash, blocked_date=None, blocked_reason="",
        )
        self._error = ""
        self._commit(candidate, replace_unverified=True)
        self._transient = ()
        self._refresh()

    def _unavailable(self, day: date) -> str:
        if self._error:
            return self._error
        if self._state is None:
            return "budget_uninitialized"
        if day < self._state.budget_date:
            return "budget_clock_reversed"
        if self._state.blocked_date is not None and day <= self._state.blocked_date:
            return self._state.blocked_reason or "budget_state_unverified"
        return ""

    def _commit(self, candidate: BudgetState, *, replace_unverified: bool = False) -> bool:
        if self._closed:
            return False
        candidate = replace(candidate, revision=candidate.revision + 1)
        try:
            self._file.write(candidate, replace_unverified=replace_unverified)
        except (OSError, ValueError, KeyError):
            # Keep the candidate conservatively charged even if replace succeeded
            # but directory synchronization failed. No HTTP follows this failure.
            day = max(candidate.budget_date, BudgetDay(self._last_at)) if self._last_at else candidate.budget_date
            self._state = replace(candidate, blocked_date=day, blocked_reason="budget_state_unwritable")
            self._error = "budget_state_unwritable"
            self._refresh()
            return False
        self._state = candidate
        self._refresh()
        return True

    def _refresh(self) -> None:
        state = self._state
        days = {item.budget_date.isoformat() for item in state.requests} if state else set()
        if state:
            days.add(state.budget_date.isoformat())
        days.update(item.budget_date.isoformat() for item in self._transient)
        snapshots = tuple(
            (day, ProjectBudgetSummary(state, self._transient, day, self._config, self._unavailable(date.fromisoformat(day))))
            for day in days
        )
        empty = ProjectBudgetSummary(None, (), "", self._config, self._error or (state.blocked_reason if state else "budget_uninitialized"))
        with self._summary_lock:
            self._summaries = snapshots
            self._empty_summary = empty


def _stage_key(strategy: Strategy, phase: str, bucket: str, requested_at: datetime) -> str:
    if bucket in {"shared_preheat", "emergency"}:
        return bucket
    if phase == "before_close":
        clock = requested_at.astimezone(ZoneInfo("Asia/Shanghai")).time().replace(tzinfo=None)
        phase = "morning_main" if clock < time(13) else ("afternoon" if clock < time(14, 20) else "final_review")
    period = "morning" if phase in {"morning_observe", "morning_main", "morning_late"} else (
        "final" if phase == "final_review" else phase
    )
    return f"{strategy.value}_{period}"


def _validate_budget_envelopes(
    daily_hard_limit: int, strategy_limits: Mapping[str, int], stage_targets: Mapping[str, int], stage_limits: Mapping[str, int]
) -> None:
    if not 0 <= daily_hard_limit <= 168 or any(value < 0 for value in (*strategy_limits.values(), *stage_limits.values(), *stage_targets.values())):
        raise ValueError("daily hard limit must be between 0 and 168; limits must be nonnegative")
    if sum(strategy_limits.values()) > daily_hard_limit:
        raise ValueError("strategy limits cannot exceed the daily hard limit")
    if set(stage_targets) != set(stage_limits) or any(stage_targets[name] > stage_limits[name] for name in stage_targets):
        raise ValueError("stage targets must match stages and fit limits")
    if sum(stage_limits.values()) > sum(strategy_limits.values()):
        raise ValueError("stage limits cannot exceed strategy limits")


__all__ = ["BudgetReservation", "DeepSeekBudgetLedger"]
