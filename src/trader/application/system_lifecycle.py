"""Process-wide startup and shutdown orchestration for composed resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trader.application.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep


class SupervisorResource(Protocol):
    def start(self) -> bool: ...

    def stop(self, deadline: ShutdownDeadline | None = None) -> ShutdownReport: ...


class PoolResource(Protocol):
    def start(self) -> bool: ...

    def stop(
        self,
        *,
        wait: bool = True,
        cancel_futures: bool = False,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep: ...


class SourceLaneResource(Protocol):
    def stop(
        self,
        *,
        wait: bool = True,
        deadline: ShutdownDeadline | None = None,
    ) -> tuple[ShutdownStep, ...]: ...


class AuxiliaryRuntimeResource(Protocol):
    def start(self) -> bool: ...

    def stop(
        self,
        *,
        wait: bool,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep: ...


class CacheResource(Protocol):
    def stop(
        self,
        *,
        wait: bool = True,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep: ...


@dataclass(frozen=True)
class SystemLifecycleResources:
    supervisor: SupervisorResource
    source_lanes: SourceLaneResource
    history_pool: PoolResource
    research_pool: PoolResource
    auxiliary_runtime: AuxiliaryRuntimeResource | None
    market_cache: CacheResource


def start_application_resources(
    resources: SystemLifecycleResources,
    *,
    timeout_seconds: float,
) -> bool:
    supervisor = resources.supervisor
    history_pool = resources.history_pool
    research_pool = resources.research_pool
    auxiliary_runtime = resources.auxiliary_runtime
    auxiliary_started = auxiliary_runtime.start() if auxiliary_runtime is not None else False
    history_started = False
    research_started = False
    try:
        history_started = history_pool.start()
        research_started = research_pool.start()
        started = supervisor.start()
    except BaseException:
        deadline = ShutdownDeadline.start(timeout_seconds)
        if research_started:
            research_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
        if history_started:
            history_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
        if auxiliary_started and auxiliary_runtime is not None:
            auxiliary_runtime.stop(wait=True, deadline=deadline)
        raise
    if started:
        return True
    if not any((research_started, history_started, auxiliary_started)):
        return False
    deadline = ShutdownDeadline.start(timeout_seconds)
    if research_started:
        research_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
    if history_started:
        history_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
    if auxiliary_started and auxiliary_runtime is not None:
        auxiliary_runtime.stop(wait=True, deadline=deadline)
    return False


def stop_application_resources(
    resources: SystemLifecycleResources,
    *,
    deadline: ShutdownDeadline,
) -> ShutdownReport:
    supervisor = resources.supervisor
    source_lanes = resources.source_lanes
    history_pool = resources.history_pool
    research_pool = resources.research_pool
    auxiliary_runtime = resources.auxiliary_runtime
    market_cache = resources.market_cache
    steps: list[ShutdownStep] = []
    if auxiliary_runtime is not None:
        auxiliary_runtime.stop(wait=False, deadline=deadline)
    source_lanes.stop(wait=False, deadline=deadline)
    supervisor_report = supervisor.stop(deadline)
    steps.extend(supervisor_report.steps)
    steps.extend(source_lanes.stop(wait=True, deadline=deadline))
    steps.append(history_pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    steps.append(research_pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    if auxiliary_runtime is not None:
        steps.append(auxiliary_runtime.stop(wait=True, deadline=deadline))
    steps.append(market_cache.stop(wait=True, deadline=deadline))
    return ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)


__all__ = ["SystemLifecycleResources", "start_application_resources", "stop_application_resources"]
