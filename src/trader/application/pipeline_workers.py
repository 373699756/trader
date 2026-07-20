"""Worker submission, persistence and status helpers for pipeline stages."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from trader.application.workers import BoundedExecutor
from trader.domain.models import FeatureSnapshot, FilterAudit

if TYPE_CHECKING:
    from trader.application.pipeline import RecommendationPipeline

_P = ParamSpec("_P")
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


def store_candidate_selection(
    pipeline: RecommendationPipeline,
    market_features: Sequence[FeatureSnapshot],
    candidates: tuple[FeatureSnapshot, ...],
    reasons: Mapping[str, int],
    details: tuple[FilterAudit, ...],
) -> None:
    pipeline._market_features = tuple(market_features)
    pipeline._candidate_codes = tuple(feature.quote.code for feature in candidates)
    pipeline._candidate_features = candidates
    pipeline._filter_reasons = reasons
    pipeline._filter_details = details
    pipeline._filtered_count = len({item.stock_code for item in details})


def submit_required(
    pipeline: RecommendationPipeline,
    pool: BoundedExecutor,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    future = pool.submit(function, *args, **kwargs)
    if future is None:
        pipeline._state.increment("worker_queue_rejections")
        raise RuntimeError("bounded worker queue is full or stopped")
    return future


def data_future(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Future[_T]:
    if not pipeline._market_data_manages_workers:
        return submit_required(pipeline, pipeline._data_pool, function, *args, **kwargs)
    future: Future[_T] = Future()
    try:
        future.set_result(function(*args, **kwargs))
    except BaseException as exc:
        future.set_exception(exc)
    return future


def persist(
    pipeline: RecommendationPipeline,
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    if not pipeline._persistence_running:
        return function(*args, **kwargs)
    future = pipeline._persistence_pool.submit(function, *args, **kwargs)
    if future is None:
        pipeline._state.increment("persistence_queue_rejections")
        raise RuntimeError("persistence queue is full or stopped")
    return future.result()


def worker_status(pipeline: RecommendationPipeline) -> dict[str, object]:
    worker = pipeline._worker
    queue_status = pipeline._queue.status()
    with pipeline._merge_status_lock:
        merge_status = {
            "workers": 1,
            "queue_capacity": queue_status["capacity"],
            "inflight": pipeline._merge_inflight,
            "submitted_count": pipeline._merge_submitted_count,
            "completed_count": pipeline._merge_completed_count,
            "rejected_count": queue_status["rejected_count"],
            "running": bool(worker is not None and worker.is_alive()),
        }
    return {
        "data": pipeline._data_pool.status(),
        "normalization": pipeline._normalization_pool.status(),
        "strategy": pipeline._strategy_pool.status(),
        "deepseek": pipeline._deepseek_pool.status(),
        "long": pipeline._long_pool.status(),
        "merge": merge_status,
        "persistence": pipeline._persistence_pool.status(),
    }
