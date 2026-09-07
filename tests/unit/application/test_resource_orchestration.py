from __future__ import annotations

from dataclasses import dataclass

from trader.application.runtime.resource_orchestration import (
    ApplicationResources,
    start_application_resources,
    stop_application_resources,
)
from trader.application.runtime.shutdown import ShutdownDeadline, ShutdownReport, ShutdownStep


@dataclass
class _Pool:
    name: str
    events: list[str]

    def start(self) -> bool:
        self.events.append(f"start:{self.name}")
        return True

    def stop(
        self,
        *,
        wait: bool = True,
        cancel_futures: bool = False,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        del cancel_futures, deadline
        self.events.append(f"stop:{self.name}:{wait}")
        return ShutdownStep(self.name, True, False)


@dataclass
class _Supervisor:
    name: str
    events: list[str]

    def start(self) -> bool:
        self.events.append(f"start:{self.name}")
        return True

    def stop(self, deadline: ShutdownDeadline | None = None) -> ShutdownReport:
        deadline = deadline or ShutdownDeadline.start(1.0)
        self.events.append("stop:supervisor")
        return ShutdownReport.from_steps(deadline, (ShutdownStep("supervisor", True, False),))


class _RefusingSupervisor(_Supervisor):
    def start(self) -> bool:
        self.events.append("start:supervisor")
        return False


class _SourceLanes:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(
        self,
        *,
        wait: bool = True,
        deadline: ShutdownDeadline | None = None,
    ) -> tuple[ShutdownStep, ...]:
        del deadline
        self._events.append(f"stop:source-lanes:{wait}")
        return (ShutdownStep("source-lanes", True, False),)


class _Cache:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stop(
        self,
        *,
        wait: bool = True,
        deadline: ShutdownDeadline | None = None,
    ) -> ShutdownStep:
        del deadline
        self._events.append(f"stop:cache:{wait}")
        return ShutdownStep("cache", True, False)


def test_quote_pool_is_started_before_supervisor_and_stopped_after_source_lanes() -> None:
    events: list[str] = []
    resources = ApplicationResources(
        supervisor=_Supervisor("supervisor", events),
        source_lanes=_SourceLanes(events),
        data_pool=_Pool("data", events),
        quote_pool=_Pool("quote", events),
        history_pool=_Pool("history", events),
        research_pool=_Pool("research", events),
        auxiliary_runtimes=(),
        market_cache=_Cache(events),
    )

    assert start_application_resources(resources, timeout_seconds=1.0) is True
    stop_application_resources(resources, deadline=ShutdownDeadline.start(1.0))

    assert events[:5] == [
        "start:data",
        "start:quote",
        "start:history",
        "start:research",
        "start:supervisor",
    ]
    assert events.index("stop:source-lanes:True") < events.index("stop:quote:True")


def test_quote_pool_is_rolled_back_when_supervisor_does_not_start() -> None:
    events: list[str] = []
    resources = ApplicationResources(
        supervisor=_RefusingSupervisor("supervisor", events),
        source_lanes=_SourceLanes(events),
        data_pool=_Pool("data", events),
        quote_pool=_Pool("quote", events),
        history_pool=_Pool("history", events),
        research_pool=_Pool("research", events),
        auxiliary_runtimes=(),
        market_cache=_Cache(events),
    )

    assert start_application_resources(resources, timeout_seconds=1.0) is False
    assert "stop:quote:True" in events
