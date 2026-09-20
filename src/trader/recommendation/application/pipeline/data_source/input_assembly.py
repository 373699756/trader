"""Input assembly and observation-time rules for the data-source adapter."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, time, timedelta
from typing import TYPE_CHECKING

from trader.recommendation.application.ports.runtime import (
    CycleRequest,
    DecisionUnavailableError,
    DataRefreshUnavailableError,
    PipelineTaskRequest,
)
from trader.recommendation.application.ports.loaded_profile import ModelScoringContext
from trader.recommendation.application.runtime.cadence import task_execution_budget_seconds
from trader.recommendation.application.runtime.schedule import SHANGHAI
from trader.recommendation.domain.market.models import FeatureSnapshot
from trader.recommendation.domain.publication.decision_identity import DecisionQuote, ScoredDecision
from trader.recommendation.domain.publication.models import Strategy

if TYPE_CHECKING:
    from trader.recommendation.application.pipeline.data_source.source_router import InputBatch


def quote_order(feature: FeatureSnapshot) -> tuple[datetime, datetime, str]:
    quote = feature.quote
    return quote.source_time, quote.received_time, quote.data_version


def selected_quote_features(batch: InputBatch, selected_codes: Collection[str]) -> dict[str, FeatureSnapshot]:
    features_by_code: dict[str, FeatureSnapshot] = {}
    for feature in (*batch.market_features, *batch.candidate_features):
        if feature.quote.code not in selected_codes:
            continue
        current = features_by_code.get(feature.quote.code)
        if current is None or quote_order(feature) > quote_order(current):
            features_by_code[feature.quote.code] = feature
    return features_by_code


def overlay_observed_at(request: CycleRequest, features: tuple[FeatureSnapshot, ...]) -> datetime:
    target_zone = request.observed_at.tzinfo
    if target_zone is None:
        raise DecisionUnavailableError("overlay_request_time_unavailable")
    values = [request.observed_at]
    for feature in features:
        values.extend((feature.observed_at, feature.quote.received_time))
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise DecisionUnavailableError("overlay_input_time_unavailable")
    observed_at = max(value.astimezone(target_zone) for value in values)
    if observed_at.date() != request.trade_date:
        raise DecisionUnavailableError("overlay_observation_trade_date_mismatch")
    return observed_at


def merge_overlay_quote(quotes: dict[str, DecisionQuote], feature: FeatureSnapshot, observed_at: datetime) -> bool:
    quote = feature.quote
    if quote.price is None or quote.price <= 0.0 or quote.source_time > observed_at:
        return False
    candidate = DecisionQuote(
        code=quote.code,
        price=quote.price,
        pct_change=quote.pct_change,
        amount=quote.amount,
        turnover_rate=quote.turnover_rate,
        market_cap=quote.market_cap,
        source=quote.source,
        source_time=quote.source_time,
        data_version=quote.data_version,
    )
    existing = quotes.get(quote.code)
    if existing is not None and (candidate.source_time, candidate.data_version) <= (
        existing.source_time,
        existing.data_version,
    ):
        return False
    quotes[quote.code] = candidate
    return True


def task_deadline(request: PipelineTaskRequest) -> datetime | None:
    seconds = task_execution_budget_seconds(request.task)
    return request.observed_at + timedelta(seconds=seconds) if seconds is not None else None


def candidate_batch_is_complete(
    requested: Mapping[Strategy, tuple[str, ...]],
    features: Mapping[Strategy, tuple[FeatureSnapshot, ...]],
) -> bool:
    if not any(requested.values()):
        return False
    return all(
        tuple(item.quote.code for item in features[strategy]) == requested[strategy]
        for strategy in (Strategy.TOMORROW, Strategy.D25)
    )


def require_codes(codes: tuple[str, ...], error_code: str) -> None:
    if not codes:
        raise DataRefreshUnavailableError(error_code)


def refresh_completed_at(request: PipelineTaskRequest, features: tuple[FeatureSnapshot, ...]) -> datetime:
    values = (
        request.observed_at,
        *(feature.observed_at for feature in features),
        *(feature.quote.received_time for feature in features),
    )
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("refresh completion times must be timezone-aware")
    return max(value.astimezone(SHANGHAI) for value in values)


def decision_observed_at(batch: InputBatch) -> datetime:
    target_zone = batch.request.observed_at.tzinfo
    if target_zone is None:
        raise ValueError("decision request time must be timezone-aware")
    values = [batch.request.observed_at]
    for feature in (*batch.market_features, *batch.candidate_features):
        values.extend((feature.observed_at, feature.quote.received_time))
        values.extend(evidence.received_at for evidence in feature.evidence if evidence.received_at is not None)
        values.extend(fact.observed_at for fact in feature.external_risk_facts)
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise ValueError("decision input times must be timezone-aware")
    return max(value.astimezone(target_zone) for value in values)


def model_scoring_context(request: CycleRequest, batch: InputBatch, now: datetime) -> ModelScoringContext:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("model scoring clock must be timezone-aware")
    local_now = now.astimezone(SHANGHAI)
    input_at = decision_observed_at(batch).astimezone(SHANGHAI)
    input_age_seconds = max(0.0, (local_now - input_at).total_seconds())
    if request.phase == "close_fallback":
        return ModelScoringContext(input_age_seconds=input_age_seconds)
    deadline = datetime.combine(request.trade_date, time(15, 0), tzinfo=SHANGHAI)
    return ModelScoringContext(
        time_budget_seconds=max(0.0, (deadline - local_now).total_seconds()),
        input_age_seconds=input_age_seconds,
    )
