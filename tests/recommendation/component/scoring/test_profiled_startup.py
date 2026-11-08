from __future__ import annotations

import threading
from datetime import datetime

import pytest

from tests.helpers.shared.bootstrap import _config
from trader.bootstrap import BuildProfiledSystem
from trader.infra.settings import LoadRuntimeSettings
from trader.infra.shutdown import ShutdownDeadline, ShutdownIncompleteError, ShutdownReport, ShutdownStep
from trader.recommendation.application.runtime.cadence import PipelineTask, ScheduledPipelineTask
from trader.recommendation.application.runtime.schedule import SHANGHAI, MarketPhase
from trader.recommendation.application.runtime.supervisor import RuntimeSupervisor
from trader.recommendation.infra.runtime.profiled_application import ProfiledApplicationSystem
from trader.recommendation.infra.scoring.profile_factory import ScoringModelsMissingError


@pytest.mark.parametrize("profiles", (("v2",), ("v3", "v5"), ("v1", "v2", "v3", "v5")))
def test_all_publication_sequences_are_ready_before_first_profile_tick(tmp_path, monkeypatch, profiles):
    now = datetime(2026, 11, 2, 10, 0, tzinfo=SHANGHAI)
    monkeypatch.setattr("trader.bootstrap._utc_now", lambda: now)
    monkeypatch.setattr("trader.bootstrap.PublishedReferences.initialize", lambda self: None)
    monkeypatch.setattr("trader.bootstrap.ChinaTradingCalendar.open_dates", lambda self: (now.date(),))

    def missing_model(*args, **kwargs):
        raise ScoringModelsMissingError()

    monkeypatch.setattr("trader.bootstrap.LoadScoringProfile", missing_model)
    system = BuildProfiledSystem(_config(tmp_path), profiles=profiles)
    errors = []
    admissions = []
    ticked = {profile: threading.Event() for profile in profiles}

    def tick(runtime, at=None):
        profile = runtime._dependencies.round_profile
        rounds = runtime._dependencies.rounds
        task = ScheduledPipelineTask(PipelineTask.SCORE, now, MarketPhase.BEFORE_CLOSE)
        try:
            admission = rounds.enter(profile, (task,))
            assert admission is not None
            admissions.append(profile)
            rounds.finish(profile, admission.sequence)
        except Exception as exc:
            errors.append(str(exc))
        finally:
            ticked[profile].set()
        return 60.0

    # Hold startup at each profile's first tick to expose the original ordering
    # deterministically, including when later profiles have slow initializers.
    original_start = RuntimeSupervisor.start

    def start_and_wait(supervisor):
        started = original_start(supervisor)
        profile = supervisor._runtime._dependencies.round_profile
        assert ticked[profile].wait(2.0)
        return started

    monkeypatch.setattr("trader.bootstrap.SchedulerRuntime.submit_due", tick)
    monkeypatch.setattr(RuntimeSupervisor, "start", start_and_wait)
    try:
        assert system.start()
        assert errors == []
        assert admissions == [profile.profile_id for profile in system.systems]
    finally:
        assert system.stop(deadline=ShutdownDeadline.start(3.0)).completed


@pytest.mark.parametrize("failure", ("initialize", "start"))
def test_profile_startup_failure_reaps_prepared_profiles_with_one_deadline(tmp_path, failure):
    events = []
    deadlines = []

    class Profile:
        def __init__(self, profile_id):
            self.profile_id = profile_id

        def initialize(self):
            events.append(f"initialize:{self.profile_id}")
            if failure == "initialize" and self.profile_id == "v3":
                raise RuntimeError("controlled_initialization_failure")

        def start(self):
            events.append(f"start:{self.profile_id}")
            return self.profile_id != "v3"

        def stop(self, *, deadline):
            events.append(f"stop:{self.profile_id}")
            deadlines.append(deadline)
            return ShutdownReport.from_steps(deadline, (ShutdownStep(self.profile_id, True, False),))

    system = ProfiledApplicationSystem(
        LoadRuntimeSettings(_config(tmp_path)), None, tuple(Profile(profile) for profile in ("v2", "v3", "v5"))
    )
    with pytest.raises(RuntimeError, match="controlled_initialization_failure|v3 runtime did not start"):
        system.start()
    if failure == "initialize":
        assert events == ["initialize:v2", "initialize:v3", "stop:v3", "stop:v2"]
    else:
        assert events == [
            "initialize:v2", "initialize:v3", "initialize:v5", "start:v2", "start:v3",
            "stop:v5", "stop:v3", "stop:v2",
        ]
    assert all(deadline is deadlines[0] for deadline in deadlines)


@pytest.mark.parametrize("failed_profile", ("v5", "v3", "v2"))
def test_profile_stop_failure_keeps_shared_owner_last_and_uses_original_deadline(tmp_path, failed_profile):
    events = []
    clock = [0.0]
    deadline = ShutdownDeadline.start(1, monotonic=lambda: clock[0])

    class Profile:
        def __init__(self, profile_id):
            self.profile_id = profile_id

        def stop(self, *, deadline):
            events.append((self.profile_id, deadline, deadline.remaining_seconds()))
            clock[0] += 0.6
            if self.profile_id == failed_profile:
                raise RuntimeError("private resource payload")
            return ShutdownReport.from_steps(deadline, (ShutdownStep("resource", True, False),))

    system = ProfiledApplicationSystem(
        LoadRuntimeSettings(_config(tmp_path)), None, tuple(Profile(profile) for profile in ("v2", "v3", "v5")),
    )
    report = system.stop(deadline=deadline)
    assert not report.completed and report.forced
    assert [item[0] for item in events] == ["v5", "v3", "v2"]
    assert all(item[1] is deadline for item in events)
    assert [item[2] for item in events] == [1.0, 0.4, 0.0]
    failure, = (step for step in report.steps if not step.completed)
    assert failure.name == f"profile:{failed_profile}"
    assert failure.detail == "shutdown_failed:RuntimeError"


def test_profile_rollback_failure_does_not_skip_first_profile_or_replace_original_cause(tmp_path):
    events = []
    original = ValueError("initialization failed")

    class Profile:
        def __init__(self, profile_id):
            self.profile_id = profile_id

        def initialize(self):
            if self.profile_id == "v3":
                raise original

        def stop(self, *, deadline):
            events.append((self.profile_id, deadline))
            if self.profile_id == "v3":
                raise RuntimeError("rollback failed")
            return ShutdownReport.from_steps(deadline, (ShutdownStep("resource", True, False),))

    system = ProfiledApplicationSystem(
        LoadRuntimeSettings(_config(tmp_path)), None, tuple(Profile(profile) for profile in ("v2", "v3", "v5")),
    )
    with pytest.raises(ShutdownIncompleteError) as caught:
        system.start()
    assert caught.value.__cause__ is original
    assert [item[0] for item in events] == ["v3", "v2"]
    assert events[0][1] is events[1][1]
    assert not caught.value.report.completed


def test_profile_rollback_reuses_deadline_from_incomplete_resource_cleanup(tmp_path):
    clock = [0.0]
    deadline = ShutdownDeadline.start(1, monotonic=lambda: clock[0])
    original = ShutdownIncompleteError(
        ShutdownReport.from_steps(deadline, (ShutdownStep("resource", False, False),)), deadline,
    )
    received = []

    class Profile:
        profile_id = "v2"

        def initialize(self):
            return None

        def start(self):
            clock[0] = 0.75
            raise original

        def stop(self, *, deadline):
            received.append(deadline)
            assert deadline.remaining_seconds() == 0.25
            return ShutdownReport.from_steps(deadline, (ShutdownStep("resource", True, False),))

    system = ProfiledApplicationSystem(LoadRuntimeSettings(_config(tmp_path)), None, (Profile(),))
    with pytest.raises(ShutdownIncompleteError) as caught:
        system.start()
    assert caught.value is original
    assert received == [deadline]
