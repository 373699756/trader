"""Pure scheduler request and runtime identity rules."""

from __future__ import annotations

import hashlib
import re
from typing import cast

from trader.application.ports.market import ResearchRefreshResult
from trader.application.ports.scheduler import CycleRequest, DecisionUnavailableError
from trader.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.application.runtime.schedule import MarketPhase
from trader.domain.recommendation.decision_identity import DecisionIdentity
from trader.domain.recommendation.models import Strategy


def cycle_phase(strategy: Strategy, phase: MarketPhase) -> str:
    if strategy in {Strategy.TOMORROW, Strategy.D25} and phase is MarketPhase.AFTER_CLOSE:
        return "close_fallback"
    if phase is MarketPhase.MIDDAY:
        return "midday_recovery"
    return cast(str, phase.value)


def validate_cycle_identity(request: CycleRequest, identity: DecisionIdentity) -> None:
    if identity.strategy is not request.strategy or identity.trade_date != request.trade_date:
        raise DecisionUnavailableError("decision identity does not match its scheduled cycle")
    if identity.observed_at < request.observed_at:
        raise DecisionUnavailableError("decision identity predates its scheduled cycle")


def cycle_order_key(request: CycleRequest) -> int:
    return request.trade_date.toordinal() * 1_000_000_000 + request.sequence


def cycle_correlation_id(request: CycleRequest) -> str:
    return f"score:{request.strategy.value}:{request.trade_date.isoformat()}:{request.sequence}"


def pipeline_task_order_key(request: ScheduledPipelineTask) -> int:
    return int(request.scheduled_at.timestamp() * 1_000_000)


def pipeline_task_correlation_id(request: ScheduledPipelineTask) -> str:
    return f"data:{request.task.value}:{request.scheduled_at.isoformat()}"


def pipeline_lane(task: PipelineTask) -> PipelineTask:
    if task in {PipelineTask.CURRENT_QUOTES, PipelineTask.CLOSE_QUOTES}:
        return PipelineTask.FULL_MARKET
    if task is PipelineTask.FINAL_CANDIDATE_QUOTES:
        return PipelineTask.CANDIDATE_QUOTES
    return task


def failure_code(exc: BaseException, fallback: str) -> str:
    value = str(exc).strip().lower().replace(" ", "_")
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None:
        return value
    return fallback


def research_input_version(result: ResearchRefreshResult) -> str:
    material = (result.data_version, result.requested_codes, result.changed_codes, result.completed_at)
    return hashlib.sha256(repr(material).encode("utf-8")).hexdigest()[:20]


__all__ = [
    "cycle_correlation_id",
    "cycle_order_key",
    "cycle_phase",
    "failure_code",
    "pipeline_lane",
    "pipeline_task_correlation_id",
    "pipeline_task_order_key",
    "research_input_version",
    "validate_cycle_identity",
]
