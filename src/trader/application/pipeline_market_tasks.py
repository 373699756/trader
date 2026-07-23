"""Deadline-bound market refresh tasks used by pipeline stages."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from trader.application.cadence import PipelineTask
from trader.application.events import EventDeadlineExpiredError
from trader.application.pipeline_workers import (
    data_future,
    store_candidate_selection,
    submit_required,
    urgent_data_future,
)
from trader.application.ports.market import MarketDataDeadlineExceededError, MarketDataUnavailableError
from trader.application.schedule import MarketPhase, trade_date_at
from trader.domain.recommendation.models import Strategy

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_P = ParamSpec("_P")
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


def maximum_age_seconds(phase: MarketPhase, strategy: Strategy | None = None) -> float:
    if strategy is Strategy.TODAY or phase in {
        MarketPhase.TODAY_OBSERVE,
        MarketPhase.TODAY_MAIN,
        MarketPhase.TODAY_LATE,
    }:
        return 20.0
    return 30.0


def _refresh_candidate_quotes_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    deadline: datetime | None = None,
) -> None:
    codes = _active_codes(pipeline)
    if not codes:
        return
    features = tuple(
        _run_urgent_market_data_task(
            pipeline,
            pipeline._quotes.refresh_candidate_quotes,
            codes,
            now,
            force=phase is MarketPhase.FINAL_QUOTE,
            deadline=deadline,
        )
    )
    candidate_set = set(pipeline._candidate_codes)
    pipeline._candidate_features = tuple(feature for feature in features if feature.quote.code in candidate_set)


def _refresh_market_news_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    deadline: datetime | None,
) -> None:
    codes = _active_codes(pipeline)
    if not codes:
        return
    _run_market_data_task(
        pipeline,
        pipeline._research.refresh_market_news,
        codes,
        now,
        deadline=deadline,
    )
    pipeline._candidate_features = tuple(
        pipeline._candidate_data.read_candidate_features(pipeline._candidate_codes, now)
    )


def _refresh_stock_risk_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    deadline: datetime | None,
) -> None:
    codes = _active_codes(pipeline)
    if codes:
        _run_market_data_task(
            pipeline,
            pipeline._research.refresh_stock_risk,
            codes,
            now,
            deadline=deadline,
        )


def _refresh_reference_data_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
) -> None:
    codes = _active_codes(pipeline)
    scheduler = pipeline._references.schedule_reference_data
    if callable(scheduler):
        scheduler(codes, now, force=phase is MarketPhase.AFTER_CLOSE)
        return
    _run_market_data_task(
        pipeline,
        pipeline._references.refresh_reference_data,
        codes,
        now,
        force=phase is MarketPhase.AFTER_CLOSE,
    )


def _active_codes(pipeline: RecommendationPipeline) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*pipeline._candidate_codes, *pipeline._long_codes)))


def _run_market_data_task(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    return data_future(pipeline, function, *args, **kwargs).result()


def _run_urgent_market_data_task(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    return urgent_data_future(pipeline, function, *args, **kwargs).result()


def _refresh_candidates_on_workers(
    pipeline: RecommendationPipeline,
    now: datetime,
    phase: MarketPhase,
    *,
    force: bool = False,
    deadline: datetime | None = None,
) -> None:
    market_started = time.perf_counter()
    market_future = data_future(
        pipeline,
        pipeline._market_full.fetch_market_features,
        now,
        force=force,
        deadline=deadline,
    )
    try:
        market_result = _event_result(
            pipeline,
            market_future,
            deadline=deadline,
            event_type=PipelineTask.FULL_MARKET.value,
        )
        market_features = tuple(market_result)
        pipeline._latency.record_duration(
            "market_fetch",
            (time.perf_counter() - market_started) * 1000.0,
        )
    except MarketDataDeadlineExceededError as exc:
        raise EventDeadlineExpiredError(
            f"event deadline expired during execution: {PipelineTask.FULL_MARKET.value}"
        ) from exc
    except MarketDataUnavailableError as exc:
        reason = str(exc)[:500]
        _LOGGER.warning("candidate refresh degraded during %s: %s", phase.value, reason)
        pipeline._state.increment("market_refresh_failures")
        pipeline._state.record_error(f"market data degraded during {phase.value}: {reason}")
        return
    preparation_started = time.perf_counter()
    selection = submit_required(
        pipeline,
        pipeline._normalization_pool,
        pipeline._engine.preselect,
        market_features,
        now=now,
        max_age_seconds=maximum_age_seconds(phase),
        limit=pipeline._candidate_pool_size,
        strategies=_short_strategies_for_phase(phase),
        trade_date=trade_date_at(now).isoformat(),
        phase=phase.value,
    )
    candidates, reasons, details = _event_result(
        pipeline,
        selection,
        deadline=deadline,
        event_type=PipelineTask.FULL_MARKET.value,
    )
    pipeline._latency.record_duration(
        "candidate_preparation",
        (time.perf_counter() - preparation_started) * 1000.0,
    )
    store_candidate_selection(pipeline, market_features, candidates, reasons, details)


def _short_strategies_for_phase(phase: MarketPhase) -> tuple[Strategy, ...]:
    if phase in {
        MarketPhase.WARMUP,
        MarketPhase.TODAY_OBSERVE,
        MarketPhase.TODAY_MAIN,
        MarketPhase.TODAY_LATE,
    }:
        return (Strategy.TODAY,)
    if phase in {MarketPhase.AFTERNOON, MarketPhase.FINAL_REVIEW, MarketPhase.FINAL_QUOTE}:
        return (Strategy.TOMORROW, Strategy.D25)
    return (Strategy.TODAY, Strategy.TOMORROW, Strategy.D25)


def _event_result(
    pipeline: RecommendationPipeline,
    future: Future[_T],
    *,
    deadline: datetime | None,
    event_type: str,
) -> _T:
    if deadline is None:
        return future.result()
    remaining = (deadline - pipeline._now()).total_seconds()
    if remaining <= 0.0:
        future.cancel()
        raise EventDeadlineExpiredError(f"event deadline expired during execution: {event_type}")
    try:
        result = future.result(timeout=remaining)
    except FutureTimeoutError as exc:
        future.cancel()
        raise EventDeadlineExpiredError(f"event deadline expired during execution: {event_type}") from exc
    if pipeline._now() >= deadline:
        raise EventDeadlineExpiredError(f"event deadline expired during execution: {event_type}")
    return result
