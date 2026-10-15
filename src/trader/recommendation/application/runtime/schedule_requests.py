"""Pure scheduler request and runtime identity rules."""

from __future__ import annotations

import re
from typing import cast

from trader.recommendation.application.ports.runtime import CycleRequest, DecisionUnavailableError
from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.schedule import MarketPhase
from trader.recommendation.domain.market.refresh import ResearchRefreshResult
from trader.recommendation.domain.publication.decision_identity import DecisionIdentity
from trader.recommendation.domain.publication.models import Strategy


def CyclePhase(strategy: Strategy, phase: MarketPhase) -> str:
    if strategy in {Strategy.TOMORROW, Strategy.D25} and phase is MarketPhase.AFTER_CLOSE:
        return "close_fallback"
    if phase is MarketPhase.MIDDAY:
        return "midday_recovery"
    return cast(str, phase.value)


def ValidateCycleIdentity(request: CycleRequest, identity: DecisionIdentity) -> None:
    if identity.strategy is not request.strategy or identity.trade_date != request.trade_date:
        raise DecisionUnavailableError("decision identity does not match its scheduled cycle")
    if identity.observed_at < request.observed_at:
        raise DecisionUnavailableError("decision identity predates its scheduled cycle")


def CycleOrderKey(request: CycleRequest) -> int:
    generation = request.session.generation if request.session is not None else 0
    return generation * 10**24 + request.trade_date.toordinal() * 1_000_000_000 + request.sequence


def CycleCorrelationId(request: CycleRequest) -> str:
    return f"score:{request.strategy.value}:{request.trade_date.isoformat()}:{request.sequence}"


def PipelineTaskOrderKey(request: ScheduledPipelineTask) -> int:
    generation = request.session.generation if request.session is not None else 0
    return generation * 10**24 + int(request.scheduled_at.timestamp() * 1_000_000)


def PipelineTaskCorrelationId(request: ScheduledPipelineTask) -> str:
    return f"data:{request.task.value}:{request.scheduled_at.isoformat()}"


def PipelineLane(task: PipelineTask) -> PipelineTask:
    if task in {PipelineTask.CURRENT_QUOTES, PipelineTask.CLOSE_QUOTES}:
        return PipelineTask.FULL_MARKET
    if task is PipelineTask.FINAL_CANDIDATE_QUOTES:
        return PipelineTask.CANDIDATE_QUOTES
    return task


def FailureCode(exc: BaseException, fallback: str) -> str:
    value = str(exc).strip().lower().replace(" ", "_")
    if re.fullmatch(r"[a-z0-9_]{1,64}", value) is not None:
        return value
    return fallback


def ResearchInputVersion(result: ResearchRefreshResult) -> str:
    return result.data_version + (f":{result.completed_at:%Y%m%dT%H%M%S%f}" if result.completed_at is not None else "")


__all__ = [
    "CycleCorrelationId",
    "CycleOrderKey",
    "CyclePhase",
    "FailureCode",
    "PipelineLane",
    "PipelineTaskCorrelationId",
    "PipelineTaskOrderKey",
    "ResearchInputVersion",
    "ValidateCycleIdentity",
]
