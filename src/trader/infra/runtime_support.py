"""Typed infrastructure resources shared by the composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from trader.application.runtime.source_lanes import SourceLaneRegistry
from trader.application.runtime.workers import BoundedExecutor
from trader.infra.cache import BoundedLruCache
from trader.infra.persistence.runtime_json import RuntimeJsonWriter

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class RuntimeWorkerResources:
    data_pool: BoundedExecutor
    history_pool: BoundedExecutor
    research_pool: BoundedExecutor
    persistence_pool: BoundedExecutor
    source_lanes: SourceLaneRegistry
    json_writer: RuntimeJsonWriter
    market_cache: BoundedLruCache[object]


@dataclass(frozen=True)
class ShanghaiClock:
    value: Callable[[], datetime]

    def now(self) -> datetime:
        return self.value().astimezone(_SHANGHAI)


__all__ = ["RuntimeWorkerResources", "ShanghaiClock"]
