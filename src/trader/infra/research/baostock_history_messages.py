"""Typed process messages for the BaoStock history runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from trader.application.research.baostock_daily import BaoStockShardContext
from trader.application.research.baostock_history_runtime import BaoStockRuntimePhase
from trader.domain.research.baostock_daily import BaoStockSecurity


@dataclass(frozen=True)
class ContextResponse:
    context: BaoStockShardContext | None
    failure_reason: str = ""


@dataclass(frozen=True)
class ContextStage:
    phase: BaoStockRuntimePhase


@dataclass(frozen=True)
class SupplierCallActivity:
    state: Literal["started", "completed"]


@dataclass(frozen=True)
class WorkerReady:
    failure_reason: str = ""


@dataclass(frozen=True)
class DownloadCommand:
    security: BaoStockSecurity


@dataclass(frozen=True)
class DownloadResponse:
    code: str
    succeeded: bool
    failure_reason: str = ""
    training_ready: bool = True


@dataclass(frozen=True)
class StopCommand:
    pass


__all__ = [
    "ContextResponse",
    "ContextStage",
    "DownloadCommand",
    "DownloadResponse",
    "StopCommand",
    "SupplierCallActivity",
    "WorkerReady",
]
