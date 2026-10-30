"""Input assembly and observation-time rules for the data-source adapter."""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, time, timedelta
import time as monotonic_time
from typing import TYPE_CHECKING

from trader.recommendation.application.ports.loaded_profile import ModelScoringContext
from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DataRefreshPendingError,
    FreezeUnavailableError,
    PipelineTaskRequest,
)
from trader.recommendation.application.runtime.cadence import PipelineTask, TaskExecutionBudgetSeconds
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.freeze_identity import FreezeQuote
from trader.recommendation.domain.publication.models import Strategy

if TYPE_CHECKING:
    from trader.recommendation.application.ports.scoring_inputs import InputBatch


def QuoteOrder(feature: FeatureSnapshot) -> tuple[datetime, datetime, str]:
    quote = feature.quote
    return quote.source_time, quote.received_time, quote.data_version


def SelectedQuoteFeatures(batch: InputBatch, selected_codes: Collection[str]) -> dict[str, FeatureSnapshot]:
    features_by_code: dict[str, FeatureSnapshot] = {}
    for feature in (*batch.market_features, *batch.candidate_features):
        if feature.quote.code not in selected_codes:
            continue
        current = features_by_code.get(feature.quote.code)
        if current is None or QuoteOrder(feature) > QuoteOrder(current):
            features_by_code[feature.quote.code] = feature
    return features_by_code


def OverlayObservedAt(request: CycleRequest, features: tuple[FeatureSnapshot, ...]) -> datetime:
    target_zone = request.observed_at.tzinfo
    if target_zone is None:
        raise FreezeUnavailableError("overlay_request_time_unavailable")
    values = [request.observed_at]
    for feature in features:
        values.extend((feature.observed_at, feature.quote.received_time))
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise FreezeUnavailableError("overlay_input_time_unavailable")
    observed_at = max(value.astimezone(target_zone) for value in values)
    if observed_at.date() != request.trade_date:
        raise FreezeUnavailableError("overlay_observation_trade_date_mismatch")
    return observed_at


def MergeOverlayQuote(quotes: dict[str, FreezeQuote], feature: FeatureSnapshot, observed_at: datetime) -> bool:
    quote = feature.quote
    if quote.source_time.tzinfo is None or quote.source_time.utcoffset() is None:
        return False
    source_time = quote.source_time.astimezone(SHANGHAI)
    if quote.price is None or quote.price <= 0.0 or source_time > observed_at:
        return False
    candidate = FreezeQuote(
        code=quote.code,
        price=quote.price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        market_cap=quote.market_cap,
        source=quote.source,
        source_time=source_time,
        data_version=quote.data_version,
    )
    existing = quotes.get(quote.code)
    if existing is not None and (candidate.source_time, candidate.data_version) < (
        existing.source_time,
        existing.data_version,
    ):
        return False
    if candidate == existing:
        return False
    quotes[quote.code] = candidate
    return True


def TaskDeadline(request: PipelineTaskRequest) -> datetime | None:
    if request.round_deadline is not None:
        return request.round_deadline
    budget_task = (
        PipelineTask.CLOSE_QUOTES
        if request.preview_context is not None
        and request.task in {PipelineTask.FULL_MARKET, PipelineTask.CANDIDATE_QUOTES}
        else request.task
    )
    seconds = TaskExecutionBudgetSeconds(budget_task)
    if seconds is None:
        return None
    return request.observed_at + timedelta(seconds=seconds)


def CandidateBatchIsComplete(
    requested: Mapping[Strategy, tuple[str, ...]],
    features: Mapping[Strategy, tuple[FeatureSnapshot, ...]],
) -> bool:
    if not any(requested.values()):
        return False
    return all(
        tuple(item.quote.code for item in features[strategy]) == requested[strategy]
        for strategy in (Strategy.TOMORROW, Strategy.D25)
    )


def RequireCodes(codes: tuple[str, ...], error_code: str) -> None:
    if not codes:
        raise DataRefreshPendingError(error_code)


def RefreshCompletedAt(request: PipelineTaskRequest, features: tuple[FeatureSnapshot, ...]) -> datetime:
    values = (
        request.observed_at,
        *(feature.observed_at for feature in features),
        *(feature.quote.received_time for feature in features),
    )
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("refresh completion times must be timezone-aware")
    return max(value.astimezone(SHANGHAI) for value in values)


def FreezeObservedAt(batch: InputBatch) -> datetime:
    target_zone = batch.request.observed_at.tzinfo
    if target_zone is None:
        raise ValueError("freeze request time must be timezone-aware")
    values = [batch.request.observed_at]
    for feature in (*batch.market_features, *batch.candidate_features):
        values.extend((feature.observed_at, feature.quote.received_time))
        values.extend(evidence.received_at for evidence in feature.evidence if evidence.received_at is not None)
        values.extend(fact.observed_at for fact in feature.external_risk_facts)
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("freeze input times must be timezone-aware")
    return max(value.astimezone(target_zone) for value in values)


def BuildModelScoringContext(
    request: CycleRequest,
    batch: InputBatch,
    now: datetime,
    monotonic: Callable[[], float] = monotonic_time.monotonic,
) -> ModelScoringContext:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("model scoring clock must be timezone-aware")
    local_now = now.astimezone(SHANGHAI)
    input_at = FreezeObservedAt(batch).astimezone(SHANGHAI)
    input_age_seconds = max(0.0, (local_now - input_at).total_seconds())
    if request.round_deadline_monotonic is not None:
        return ModelScoringContext(
            time_budget_seconds=max(0.0, request.round_deadline_monotonic - monotonic()),
            input_age_seconds=input_age_seconds,
        )
    if request.preview_context is not None:
        return ModelScoringContext(
            time_budget_seconds=TaskExecutionBudgetSeconds(PipelineTask.SCORE),
            input_age_seconds=input_age_seconds,
        )
    if request.phase == "close_fallback":
        return ModelScoringContext(input_age_seconds=input_age_seconds)
    deadline = datetime.combine(request.trade_date, time(15, 0), tzinfo=SHANGHAI)
    return ModelScoringContext(
        time_budget_seconds=max(0.0, (deadline - local_now).total_seconds()),
        input_age_seconds=input_age_seconds,
    )
