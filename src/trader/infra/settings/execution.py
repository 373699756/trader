"""Parse recommendation execution limits without constructing runtime resources."""

from __future__ import annotations

from collections.abc import Mapping

from trader.infra.settings.models import ExecutionSettings, ExecutorSettings, PriorityExecutorSettings
from trader.infra.settings.parser import Integer, RequireExactKeys, RequireMapping


def ParseExecutionSettings(raw: Mapping[str, object]) -> ExecutionSettings:
    RequireExactKeys(
        raw,
        {"control", "source_data", "candidate_quotes", "market_quotes", "intraday_minutes", "http_workers"},
        "pipeline.executors",
    )
    return ExecutionSettings(
        control=_priority_executor(RequireMapping(raw, "control"), "control", minimum_urgent=1),
        source_data=_priority_executor(RequireMapping(raw, "source_data"), "source_data", minimum_urgent=0),
        candidate_quotes=_executor(RequireMapping(raw, "candidate_quotes"), "candidate_quotes"),
        market_quotes=_executor(RequireMapping(raw, "market_quotes"), "market_quotes"),
        intraday_minutes=_executor(RequireMapping(raw, "intraday_minutes"), "intraday_minutes"),
        http_workers=Integer(raw, "http_workers", minimum=1),
    )


def _executor(raw: Mapping[str, object], name: str) -> ExecutorSettings:
    RequireExactKeys(raw, {"workers", "queue_capacity"}, f"pipeline.executors.{name}")
    return ExecutorSettings(
        workers=Integer(raw, "workers", minimum=1),
        queue_capacity=Integer(raw, "queue_capacity", minimum=0),
    )


def _priority_executor(
    raw: Mapping[str, object],
    name: str,
    *,
    minimum_urgent: int,
) -> PriorityExecutorSettings:
    RequireExactKeys(raw, {"normal_workers", "urgent_workers", "queue_capacity"}, f"pipeline.executors.{name}")
    return PriorityExecutorSettings(
        normal_workers=Integer(raw, "normal_workers", minimum=1),
        urgent_workers=Integer(raw, "urgent_workers", minimum=minimum_urgent),
        queue_capacity=Integer(raw, "queue_capacity", minimum=0),
    )


__all__ = ["ParseExecutionSettings"]
