from __future__ import annotations

from dataclasses import dataclass

import pytest

from trader.infra.shutdown import ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownStep
from trader.recommendation.application.runtime.resource_orchestration import (
    ApplicationResources,
    StartApplicationResources,
    StopApplicationResources,
)


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


@dataclass
class _Budget:
    events: list[str]

    def close(self) -> None:
        self.events.append("close:budget")


def test_quote_pool_is_started_before_supervisor_and_stopped_after_source_lanes() -> None:
    events: list[str] = []
    resources = ApplicationResources(
        supervisor=_Supervisor("supervisor", events),
        source_lanes=_SourceLanes(events),
        data_pool=_Pool("data", events),
        quote_pool=_Pool("quote", events),
        market_quote_pool=_Pool("market-quote", events),
        minute_pool=_Pool("minute", events),
        auxiliary_runtimes=(),
        market_cache=_Cache(events),
        budget=_Budget(events),
    )

    assert StartApplicationResources(resources, timeout_seconds=1.0) is True
    StopApplicationResources(resources, deadline=ShutdownDeadline.start(1.0))

    assert events[:5] == [
        "start:data",
        "start:quote",
        "start:market-quote",
        "start:minute",
        "start:supervisor",
    ]
    assert events.index("stop:source-lanes:True") < events.index("stop:quote:True")
    assert events.index("stop:source-lanes:True") < events.index("stop:market-quote:True")


def test_quote_pool_is_rolled_back_when_supervisor_does_not_start() -> None:
    events: list[str] = []
    resources = ApplicationResources(
        supervisor=_RefusingSupervisor("supervisor", events),
        source_lanes=_SourceLanes(events),
        data_pool=_Pool("data", events),
        quote_pool=_Pool("quote", events),
        market_quote_pool=_Pool("market-quote", events),
        minute_pool=_Pool("minute", events),
        auxiliary_runtimes=(),
        market_cache=_Cache(events),
        budget=_Budget(events),
    )

    assert StartApplicationResources(resources, timeout_seconds=1.0) is False
    assert "stop:quote:True" in events
    assert "stop:market-quote:True" in events


def test_second_profile_does_not_start_or_close_shared_resources():
    events = []
    resources = ApplicationResources(
        supervisor=_Supervisor("v3", events),
        source_lanes=_SourceLanes(events),
        data_pool=_Pool("data", events),
        quote_pool=_Pool("quote", events),
        market_quote_pool=_Pool("market", events),
        minute_pool=_Pool("minutes", events),
        auxiliary_runtimes=(),
        market_cache=_Cache(events),
        budget=_Budget(events),
        owns_shared_resources=False,
    )
    assert StartApplicationResources(resources, timeout_seconds=1)
    StopApplicationResources(resources, deadline=ShutdownDeadline.start(1))
    assert events == ["start:v3", "stop:supervisor"]


def _resources(events: list[str]) -> ApplicationResources:
    return ApplicationResources(
        _Supervisor("supervisor", events), _SourceLanes(events),
        _Pool("data", events), _Pool("quote", events), _Pool("market", events), _Pool("minute", events),
        (_Pool("auxiliary", events),), _Cache(events), _Budget(events),
    )


@pytest.mark.parametrize("failed_owner", (
    "auxiliary_close", "source_lanes_close", "supervisor", "source_lanes_wait",
    "data_pool", "quote_pool", "market_quote_pool", "minute_pool", "auxiliary_wait", "market_cache", "budget",
))
def test_shutdown_attempts_remaining_owners_and_reports_safe_failure_chain(monkeypatch, failed_owner):
    events: list[str] = []
    resources = _resources(events)
    deadlines: list[ShutdownDeadline | None] = []
    owner, method, failed_wait = {
        "auxiliary_close": (resources.auxiliary_runtimes[0], "stop", False),
        "auxiliary_wait": (resources.auxiliary_runtimes[0], "stop", True),
        "source_lanes_close": (resources.source_lanes, "stop", False),
        "source_lanes_wait": (resources.source_lanes, "stop", True),
        "supervisor": (resources.supervisor, "stop", None),
        "data_pool": (resources.data_pool, "stop", None),
        "quote_pool": (resources.quote_pool, "stop", None),
        "market_quote_pool": (resources.market_quote_pool, "stop", None),
        "minute_pool": (resources.minute_pool, "stop", None),
        "market_cache": (resources.market_cache, "stop", None),
        "budget": (resources.budget, "close", None),
    }[failed_owner]
    original = getattr(owner, method)

    def fail(*args, **kwargs):
        deadlines.append(kwargs.get("deadline", args[0] if args else None))
        result = original(*args, **kwargs)
        if failed_wait is None or kwargs["wait"] is failed_wait:
            try:
                raise OSError("sensitive original payload")
            except OSError as exc:
                raise RuntimeError("sensitive wrapper payload") from exc
        return result

    monkeypatch.setattr(owner, method, fail)
    deadline = ShutdownDeadline.start(1)
    report = StopApplicationResources(resources, deadline=deadline)
    assert not report.completed
    assert events == [
        "stop:auxiliary:False", "stop:source-lanes:False", "stop:supervisor", "stop:source-lanes:True",
        "stop:data:True", "stop:quote:True", "stop:market:True", "stop:minute:True",
        "stop:auxiliary:True", "stop:cache:True", "close:budget",
    ]
    failures = tuple(step for step in report.steps if not step.completed)
    assert len(failures) == 1
    assert failures[0].detail == "shutdown_failed:RuntimeError<-OSError"
    assert all(item is deadline for item in deadlines if item is not None)


def test_startup_failure_preserves_original_cause_and_attempts_all_rollback_owners(monkeypatch):
    events: list[str] = []
    resources = _resources(events)
    startup_failure = ValueError("controlled startup failure")

    def failed_start():
        raise startup_failure

    def failed_stop(**kwargs):
        events.append("stop:minute:failed")
        raise RuntimeError("controlled rollback failure")

    monkeypatch.setattr(resources.supervisor, "start", failed_start)
    monkeypatch.setattr(resources.minute_pool, "stop", failed_stop)
    with pytest.raises(ShutdownIncompleteError) as caught:
        StartApplicationResources(resources, timeout_seconds=1)
    assert caught.value.__cause__ is startup_failure
    assert not caught.value.report.completed
    assert events[-11:] == [
        "stop:auxiliary:False", "stop:source-lanes:False", "stop:supervisor", "stop:source-lanes:True",
        "stop:minute:failed", "stop:data:True", "stop:quote:True", "stop:market:True",
        "stop:auxiliary:True", "stop:cache:True", "close:budget",
    ]


def test_partial_pool_start_is_rolled_back_and_later_unstarted_pools_are_untouched(monkeypatch):
    events: list[str] = []
    resources = _resources(events)
    failure = RuntimeError("partial pool startup")

    def fail():
        events.append("start:quote:partial")
        raise failure

    monkeypatch.setattr(resources.quote_pool, "start", fail)
    with pytest.raises(RuntimeError) as caught:
        StartApplicationResources(resources, timeout_seconds=1)
    assert caught.value is failure
    assert events == [
        "start:auxiliary", "start:data", "start:quote:partial", "stop:auxiliary:False", "stop:data:True", "stop:quote:True",
        "stop:auxiliary:True", "close:budget",
    ]


@pytest.mark.parametrize("forced", (False, True))
def test_incomplete_supervisor_report_cannot_become_success_after_other_resources_close(monkeypatch, forced):
    events: list[str] = []
    resources = _resources(events)

    def incomplete(deadline):
        return ShutdownReport(False, forced, deadline.elapsed_seconds, ())

    monkeypatch.setattr(resources.supervisor, "stop", incomplete)
    report = StopApplicationResources(resources, deadline=ShutdownDeadline.start(1))
    assert not report.completed
    assert report.forced is forced
    assert events[-1] == "close:budget"
    assert any(step.detail == "supervisor_shutdown_incomplete" for step in report.steps)


def test_refused_start_surfaces_incomplete_rollback_report_and_attempts_budget_close(monkeypatch):
    events: list[str] = []
    resources = _resources(events)
    monkeypatch.setattr(resources.supervisor, "start", lambda: False)
    monkeypatch.setattr(resources.minute_pool, "stop", lambda **kwargs: ShutdownStep("minute", False, False))
    with pytest.raises(ShutdownIncompleteError) as caught:
        StartApplicationResources(resources, timeout_seconds=1)
    assert any(step.name == "minute" and not step.completed for step in caught.value.report.steps)
    assert events[-1] == "close:budget"
