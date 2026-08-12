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
    data_pool: PoolResource
    history_pool: PoolResource
    research_pool: PoolResource
    auxiliary_runtimes: tuple[AuxiliaryRuntimeResource, ...]
    market_cache: CacheResource


@dataclass(frozen=True)
class _StartedResources:
    auxiliary: tuple[AuxiliaryRuntimeResource, ...]
    data_pool: bool
    history_pool: bool
    research_pool: bool


def start_application_resources(
    resources: SystemLifecycleResources,
    *,
    timeout_seconds: float,
) -> bool:
    supervisor = resources.supervisor
    history_pool = resources.history_pool
    data_pool = resources.data_pool
    research_pool = resources.research_pool
    auxiliary_runtimes = resources.auxiliary_runtimes
    auxiliary_started: list[AuxiliaryRuntimeResource] = []
    history_started = False
    data_started = False
    research_started = False
    try:
        for runtime in auxiliary_runtimes:
            if runtime.start():
                auxiliary_started.append(runtime)
        data_started = data_pool.start()
        history_started = history_pool.start()
        research_started = research_pool.start()
        started = supervisor.start()
    except BaseException:
        deadline = ShutdownDeadline.start(timeout_seconds)
        _stop_started_resources(
            resources,
            _StartedResources(tuple(auxiliary_started), data_started, history_started, research_started),
            deadline,
        )
        raise
    if started:
        return True
    if not any((research_started, history_started, data_started, bool(auxiliary_started))):
        return False
    deadline = ShutdownDeadline.start(timeout_seconds)
    _stop_started_resources(
        resources,
        _StartedResources(tuple(auxiliary_started), data_started, history_started, research_started),
        deadline,
    )
    return False


def _stop_started_resources(
    resources: SystemLifecycleResources,
    started: _StartedResources,
    deadline: ShutdownDeadline,
) -> None:
    if started.research_pool:
        resources.research_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
    if started.data_pool:
        resources.data_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
    if started.history_pool:
        resources.history_pool.stop(wait=True, cancel_futures=True, deadline=deadline)
    for runtime in reversed(started.auxiliary):
        runtime.stop(wait=True, deadline=deadline)


def stop_application_resources(
    resources: SystemLifecycleResources,
    *,
    deadline: ShutdownDeadline,
) -> ShutdownReport:
    supervisor = resources.supervisor
    source_lanes = resources.source_lanes
    history_pool = resources.history_pool
    data_pool = resources.data_pool
    research_pool = resources.research_pool
    auxiliary_runtimes = resources.auxiliary_runtimes
    market_cache = resources.market_cache
    steps: list[ShutdownStep] = []
    for runtime in auxiliary_runtimes:
        runtime.stop(wait=False, deadline=deadline)
    source_lanes.stop(wait=False, deadline=deadline)
    supervisor_report = supervisor.stop(deadline)
    steps.extend(supervisor_report.steps)
    steps.extend(source_lanes.stop(wait=True, deadline=deadline))
    steps.append(history_pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    steps.append(data_pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    steps.append(research_pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    for runtime in auxiliary_runtimes:
        steps.append(runtime.stop(wait=True, deadline=deadline))
    steps.append(market_cache.stop(wait=True, deadline=deadline))
    return ShutdownReport.from_steps(deadline, steps, forced=deadline.expired)


__all__ = ["SystemLifecycleResources", "start_application_resources", "stop_application_resources"]
