"""Process-wide startup and shutdown orchestration for composed resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from trader.infra.shutdown import (
    CollectShutdownFailures, ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownStep,
)


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


class BudgetResource(Protocol):
    def close(self) -> None: ...


@dataclass(frozen=True)
class ApplicationResources:
    supervisor: SupervisorResource
    source_lanes: SourceLaneResource
    data_pool: PoolResource
    quote_pool: PoolResource
    market_quote_pool: PoolResource
    minute_pool: PoolResource
    auxiliary_runtimes: tuple[AuxiliaryRuntimeResource, ...]
    market_cache: CacheResource
    budget: BudgetResource
    owns_shared_resources: bool = True


@dataclass(frozen=True)
class _StartedResources:
    auxiliary: tuple[AuxiliaryRuntimeResource, ...]
    data_pool: bool
    quote_pool: bool
    market_quote_pool: bool
    minute_pool: bool
    supervisor: bool


def StartApplicationResources(
    resources: ApplicationResources,
    *,
    timeout_seconds: float,
) -> bool:
    supervisor = resources.supervisor
    data_pool = resources.data_pool
    quote_pool = resources.quote_pool
    market_quote_pool = resources.market_quote_pool
    minute_pool = resources.minute_pool
    auxiliary_runtimes = resources.auxiliary_runtimes
    auxiliary_started: list[AuxiliaryRuntimeResource] = []
    data_started = False
    quote_started = False
    market_quote_started = False
    minute_started = False
    supervisor_prepared = False
    try:
        for runtime in auxiliary_runtimes:
            auxiliary_started.append(runtime)
            if not runtime.start():
                auxiliary_started.pop()
        if resources.owns_shared_resources:
            data_started = True
            data_started = data_pool.start()
            quote_started = True
            quote_started = quote_pool.start()
            market_quote_started = True
            market_quote_started = market_quote_pool.start()
            minute_started = True
            minute_started = minute_pool.start()
        supervisor_prepared = True
        started = supervisor.start()
    except BaseException as exc:
        deadline = ShutdownDeadline.start(timeout_seconds)
        report = _stop_started_resources(
            resources,
            _StartedResources(
                tuple(auxiliary_started),
                data_started,
                quote_started,
                market_quote_started,
                minute_started,
                supervisor_prepared,
            ),
            deadline,
        )
        if report.steps and not report.completed and isinstance(exc, Exception):
            raise ShutdownIncompleteError(report, deadline) from exc
        raise
    if started:
        return True
    if not any((minute_started, market_quote_started, quote_started, data_started, bool(auxiliary_started))):
        return False
    deadline = ShutdownDeadline.start(timeout_seconds)
    report = _stop_started_resources(
        resources,
        _StartedResources(
            tuple(auxiliary_started),
            data_started,
            quote_started,
            market_quote_started,
            minute_started,
            supervisor_prepared,
        ),
        deadline,
    )
    if not report.completed:
        raise ShutdownIncompleteError(report, deadline)
    return False


def _stop_started_resources(
    resources: ApplicationResources,
    started: _StartedResources,
    deadline: ShutdownDeadline,
) -> ShutdownReport:
    steps: list[ShutdownStep] = []
    forced = False
    for index, runtime in reversed(tuple(enumerate(started.auxiliary))):
        with CollectShutdownFailures(deadline, steps, f"resources:auxiliary_close:{index}"):
            runtime.stop(wait=False, deadline=deadline)
    if started.supervisor and resources.owns_shared_resources:
        with CollectShutdownFailures(deadline, steps, "resources:source_lanes_close"):
            resources.source_lanes.stop(wait=False, deadline=deadline)
    if started.supervisor:
        with CollectShutdownFailures(deadline, steps, "resources:supervisor"):
            report = resources.supervisor.stop(deadline)
            steps.extend(report.steps)
            forced = report.forced
            if not report.completed and all(step.completed for step in report.steps):
                steps.append(ShutdownStep("supervisor", False, report.forced, detail="supervisor_shutdown_incomplete"))
    if started.supervisor and resources.owns_shared_resources:
        with CollectShutdownFailures(deadline, steps, "resources:source_lanes"):
            steps.extend(resources.source_lanes.stop(wait=True, deadline=deadline))
    for name, pool, was_started in (
        ("minute_pool", resources.minute_pool, started.minute_pool),
        ("data_pool", resources.data_pool, started.data_pool),
        ("quote_pool", resources.quote_pool, started.quote_pool),
        ("market_quote_pool", resources.market_quote_pool, started.market_quote_pool),
    ):
        if was_started:
            with CollectShutdownFailures(deadline, steps, f"resources:{name}"):
                steps.append(pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    for index, runtime in reversed(tuple(enumerate(started.auxiliary))):
        with CollectShutdownFailures(deadline, steps, f"resources:auxiliary_wait:{index}"):
            steps.append(runtime.stop(wait=True, deadline=deadline))
    if resources.owns_shared_resources:
        if started.supervisor:
            with CollectShutdownFailures(deadline, steps, "resources:market_cache"):
                steps.append(resources.market_cache.stop(wait=True, deadline=deadline))
        _close_budget(resources.budget, deadline, steps)
    return ShutdownReport.from_steps(deadline, steps, forced=forced or deadline.expired)


def _close_budget(budget: BudgetResource, deadline: ShutdownDeadline, steps: list[ShutdownStep]) -> None:
    with CollectShutdownFailures(deadline, steps, "resources:deepseek_budget"):
        budget.close()
        steps.append(ShutdownStep("deepseek_budget", True, False))


def StopApplicationResources(
    resources: ApplicationResources,
    *,
    deadline: ShutdownDeadline,
) -> ShutdownReport:
    supervisor = resources.supervisor
    source_lanes = resources.source_lanes
    data_pool = resources.data_pool
    quote_pool = resources.quote_pool
    market_quote_pool = resources.market_quote_pool
    minute_pool = resources.minute_pool
    auxiliary_runtimes = resources.auxiliary_runtimes
    market_cache = resources.market_cache
    steps: list[ShutdownStep] = []
    forced = False
    for index, runtime in enumerate(auxiliary_runtimes):
        with CollectShutdownFailures(deadline, steps, f"resources:auxiliary_close:{index}"):
            runtime.stop(wait=False, deadline=deadline)
    if resources.owns_shared_resources:
        with CollectShutdownFailures(deadline, steps, "resources:source_lanes_close"):
            source_lanes.stop(wait=False, deadline=deadline)
    with CollectShutdownFailures(deadline, steps, "resources:supervisor"):
        supervisor_report = supervisor.stop(deadline)
        steps.extend(supervisor_report.steps)
        forced = supervisor_report.forced
        if not supervisor_report.completed and all(step.completed for step in supervisor_report.steps):
            steps.append(ShutdownStep("supervisor", False, supervisor_report.forced,
                                      detail="supervisor_shutdown_incomplete"))
    if resources.owns_shared_resources:
        with CollectShutdownFailures(deadline, steps, "resources:source_lanes"):
            steps.extend(source_lanes.stop(wait=True, deadline=deadline))
        for name, pool in (
            ("data_pool", data_pool), ("quote_pool", quote_pool),
            ("market_quote_pool", market_quote_pool), ("minute_pool", minute_pool),
        ):
            with CollectShutdownFailures(deadline, steps, f"resources:{name}"):
                steps.append(pool.stop(wait=True, cancel_futures=True, deadline=deadline))
    for index, runtime in enumerate(auxiliary_runtimes):
        with CollectShutdownFailures(deadline, steps, f"resources:auxiliary_wait:{index}"):
            steps.append(runtime.stop(wait=True, deadline=deadline))
    if resources.owns_shared_resources:
        with CollectShutdownFailures(deadline, steps, "resources:market_cache"):
            steps.append(market_cache.stop(wait=True, deadline=deadline))
        _close_budget(resources.budget, deadline, steps)
    return ShutdownReport.from_steps(deadline, steps, forced=forced or deadline.expired)


__all__ = ["ApplicationResources", "StartApplicationResources", "StopApplicationResources"]
