"""Typed infrastructure resources shared by the composition root."""

from __future__ import annotations

from dataclasses import dataclass

from trader.infra.atomic_files.json import RuntimeJsonWriter
from trader.infra.cache import BoundedLruCache
from trader.recommendation.application.runtime.source_lanes import SourceLaneRegistry
from trader.recommendation.application.runtime.workers import BoundedExecutor


@dataclass(frozen=True)
class RuntimeWorkerResources:
    data_pool: BoundedExecutor
    quote_pool: BoundedExecutor
    research_pool: BoundedExecutor
    persistence_pool: BoundedExecutor
    source_lanes: SourceLaneRegistry
    json_writer: RuntimeJsonWriter
    market_cache: BoundedLruCache[object]


__all__ = ["RuntimeWorkerResources"]
